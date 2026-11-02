import adsk.core
import adsk.drawing
import adsk.fusion
import traceback
from pathlib import Path
from datetime import datetime
from typing import NamedTuple, List, Set, Dict
from enum import Enum, StrEnum
from dataclasses import dataclass
import hashlib
import re
from collections import defaultdict
import itertools
import json
import os
import time
import gc
from functools import partial
import zipfile
import base64
from contextlib import contextmanager
from uuid import uuid4
try:
    from .backup_catalog import BackupCatalog, process_session_id
except ImportError:
    from backup_catalog import BackupCatalog, process_session_id

# If you have a bunch of files already existing and really want the files' date-modified attr
# to be correct but don't want to rerun an export, you can change this to True for a single run, then
# probably change it back so you're not spamming the pointless attr change every time
update_existing_file_times = False

# Older versions of this script used '_' as seperator but Fusion 360 uses ' ' per default in manual exports.
VERSION_SEPARATOR = '_' # use either ' ' or '_'

log_file = None
log_fh = None

handlers = []
options_closed = False
pending_export = None
# map from presentation of `project/folder` shown in UI to (project id, folder id)
# and also from `project` to (project id, None) if subfolders not enabled
# this is kinda hacky but not sure how reliable keying on the list item itself is
project_folders_d = {} # {f'{project.name}/{folder.name}': (project.id, folder.id)}

last_settings_path = Path(__file__).parent / 'last_settings.json'

def log(*args):
    if log_fh is None or log_fh.closed:
        print(*args)
        return
    print(*args, file=log_fh)
    log_fh.flush()

def log_ui_resources():
    """Record Windows UI handle counts to help diagnose long-batch crashes."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        user32.GetGuiResources.restype = wintypes.DWORD
        process = kernel32.GetCurrentProcess()
        counts = (user32.GetGuiResources(process, 0), user32.GetGuiResources(process, 1))
        log(f'UI resources: GDI={counts[0]} USER={counts[1]}')
        return counts
    except Exception:
        # Diagnostics must not prevent an export.
        log(f'Could not read UI resource counts: {traceback.format_exc()}')

def settle_documents():
    # Release Python wrappers before giving Fusion a chance to process deferred
    # document/UI cleanup. Do this between documents, never during an export.
    gc.collect()
    adsk.doEvents()
    time.sleep(0.05)
    adsk.doEvents()
    log_ui_resources()

def init_directory(name):
    directory = Path(name)
    directory.mkdir(exist_ok=True, parents=True)
    return directory

def init_logging(directory):
    global log_file, log_fh
    log_file = directory / '{:%Y_%m_%d_%H_%M}.txt'.format(datetime.now())
    log_fh = open(log_file, 'a', encoding="utf-8")

def load_last_settings():
    if not last_settings_path.exists():
        return {}
    with open(last_settings_path) as fh:
        return json.load(fh)

def save_last_settings(d):
    with open(last_settings_path, 'w') as fh:
        json.dump(d, fh, indent=2)

def validate_export_file(path: Path, allow_empty=False):
    if not path.is_file() or (not allow_empty and path.stat().st_size == 0):
        raise RuntimeError(f'Export did not produce a valid output file: {path}')

@contextmanager
def atomic_write(dst: Path, allow_empty=False):
    # keep the real extension last: Fusion's exporters append their own extension
    # when the path doesn't already end in one (foo.step.tmp -> foo.step.tmp.stp)
    # A fresh name prevents an interrupted run's partial file from being reused.
    tmp = dst.with_name(f'{dst.stem}.{uuid4().hex}.tmp{dst.suffix}')
    try:
        yield tmp
        validate_export_file(tmp, allow_empty)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)

# Having f3d first dictates the order we process calls to export_file, and we want f3d first so that
# things aren't unhidden when we take the thumbnail
class Format(Enum):
    F3D = 'f3d'
    STEP = 'step'
    STL = 'stl'
    IGES = 'igs'
    SAT = 'sat'
    SMT = 'smt'
    TMF = '3mf'
    PDF = 'pdf'

FormatFromName = {x.value: x for x in Format}

DEFAULT_SELECTED_FORMATS = {Format.F3D.value, Format.STEP.value}

archive_extensions = ['.zip', '.rar', '.gz', '.tar.gz', '.tar.bz2', '.tar.xz']

class Ctx(NamedTuple):
    app: adsk.core.Application
    folder: Path
    formats: List[Format]
    projects_folders: Dict[str, List[str]] # {projectId: [folderId+]} empty list is taken to mean "no filter"
    use_active_folder: bool
    unhide_all: bool
    save_sketches: bool
    num_versions: int # -1 means all versions
    export_non_design_files: bool
    subfolder_per_model: bool = False  # defaulted so settings saved before this option still load
    max_documents: int = 450
    batch: object = None  # runtime state, never serialized

    def extend(self, other):
        return self._replace(folder=self.folder / other)

    def to_dict(self):
        d = self._asdict()
        d.pop('app')
        d.pop('batch')
        d['folder'] = str(d['folder'])
        d['formats'] = [x.value for x in d['formats']]
        d['projects_folders'] = {k: list(v) for k, v in d['projects_folders'].items()}
        return d

    def dumps(self):
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, d, app):
        d = dict(d)
        d['app'] = app
        d['folder'] = Path(d['folder'])
        d['formats'] = [FormatFromName[x] for x in d['formats']]
        d['projects_folders'] = {k: list(v) for k, v in d['projects_folders'].items()}
        return cls(**d)

    def has_show_folders(self):
        return any(len(v) > 0 for v in self.projects_folders.values())

class DocumentCloseError(RuntimeError):
    """Stop the batch if Fusion cannot release a document."""

class BatchLimitReached(RuntimeError):
    pass

class BackupBatch:
    def __init__(self, catalog, limit):
        self.catalog = catalog
        self.limit = limit
        self.session = process_session_id()
        self.opened = 0
        self.outputs = []

    def before_open(self):
        resources = log_ui_resources()
        if resources and max(resources) >= 7000:
            raise BatchLimitReached('Fusion UI resource use is high')
        if not self.catalog.reserve_open(self.session, self.limit):
            raise BatchLimitReached(f'The limit of {self.limit} document opens in this Fusion session was reached')
        self.opened += 1

    def record_output(self, path):
        self.outputs.append(str(path.relative_to(self.catalog.root)))

def record_output(ctx, path):
    if ctx.batch is not None:
        ctx.batch.record_output(path)

class LazyDocument:
    def __init__(self, ctx: Ctx, file: adsk.core.DataFile):
        self._ctx = ctx
        self._document = None
        self._activated = False
        self.file = file
        self.unhidden = False

    def open(self):
        if self._document is not None and self._activated:
            return
        if self._document is None:
            # Model exports use the document's own design; creating and activating
            # a UI tab for every version needlessly exercises Fusion's UI. Drawing
            # PDF export still needs a visible, active drawing.
            visible = self.file.fileExtension == 'f2d'
            if self._ctx.batch is not None:
                self._ctx.batch.before_open()
            else:
                log_ui_resources()
            log(f'Opening `{self.file.name}` v{self.file.versionNumber} (visible={visible})')
            self._document = self._ctx.app.documents.open(self.file, visible)
        if self._document is None:
            raise RuntimeError(f'Fusion could not open `{self.file.name}` v{self.file.versionNumber}')
        if self.file.fileExtension == 'f2d':
            log(f'Activating `{self.file.name}` v{self.file.versionNumber}')
            if not self._document.activate():
                raise RuntimeError(f'Fusion could not activate `{self.file.name}` v{self.file.versionNumber}')
        self._activated = True
        log(f'Opened `{self.file.name}` v{self.file.versionNumber}')

    def unhide_all(self):
        if self.unhidden:
            return
        log(f'Unhiding bodies in `{self.file.name}` v{self.file.versionNumber}')
        unhide_all_in_document(self._document)
        self.unhidden = True
        log(f'Finished unhiding bodies in `{self.file.name}` v{self.file.versionNumber}')

    def close(self):
        if self._document is None:
            return
        log(f'Closing `{self.file.name}` v{self.file.versionNumber}')
        error = f'Stopping export because `{self.file.name}` v{self.file.versionNumber} could not be closed'
        try:
            closed = self._document.close(False)  # don't save changes
        except Exception as exc:
            raise DocumentCloseError(error) from exc
        if not closed:
            raise DocumentCloseError(error)
        self._document = None
        self._activated = False
        self.unhidden = False
        log(f'Closed `{self.file.name}` v{self.file.versionNumber}')
        settle_documents()

    @property
    def design(self):
        return design_from_document(self._document)

    @property
    def rootComponent(self):
        return self.design.rootComponent

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

@dataclass
class Counter:
    saved: int = 0
    skipped: int = 0
    errored: int = 0

    def __add__(self, other):
        return Counter(
            self.saved + other.saved,
            self.skipped + other.skipped,
            self.errored + other.errored,
        )
    def __iadd__(self, other):
        self.saved += other.saved
        self.skipped += other.skipped
        self.errored += other.errored
        return self

@dataclass
class BackupResult(Counter):
    total: int = 0
    completed: int = 0
    remaining: int = 0
    failed: int = 0
    opened: int = 0
    session_opens: int = 0
    stop_reason: str = ''
    catalog_path: str = ''

def design_from_document(document: adsk.core.Document):
    return adsk.fusion.FusionDocument.cast(document).design

def unhide_all_in_document(document: adsk.core.Document):
    unhide_all_in_component(design_from_document(document).rootComponent)

def show(item, attr='isLightBulbOn'):
    """
    Some items can't be changed (eg in externally referenced components), so log and keep going instead
    of letting one failure stop every non-f3d export of the document
    """
    try:
        # only write when hidden: each write is a change to the design that Fusion has to process, and most
        # things are already visible
        if not getattr(item, attr):
            setattr(item, attr, True)
    except Exception:
        log(f'Could not set {attr} on `{getattr(item, "name", item)}`\n{traceback.format_exc()}')

def unhide_all_in_component(component):
    show(component, 'isBodiesFolderLightBulbOn')
    show(component, 'isSketchFolderLightBulbOn')

    for brep in component.bRepBodies:
        show(brep)

    for body in component.meshBodies:
        show(body)

    # I find the name occurrences very confusing, but apparently that is what a sub-component is called
    for occurrence in component.occurrences:
        show(occurrence)
        unhide_all_in_component(occurrence.component)

def sanitize_filename(name: str) -> str:
    """
    Remove "bad" characters from a filename. Right now just punctuation that Windows doesn't like
    If any chars are removed, we append _{hash} so that we don't accidentally clobber other files
    since eg `Model 1/2` and `Model 1 2` would otherwise have the same name
    """
    # this list of characters is just from trying to rename a file in Explorer (on Windows)
    # I think the actual requirements are per fileystem and will be different on Mac
    # I'm not sure how other unicode chars are handled
    with_replacement = re.sub(r'[:\\/*?<>|"]', ' ', name)
    if name == with_replacement:
        return name
    log(f'filename `{name}` contained bad chars, replacing by `{with_replacement}`')
    hash = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f'{with_replacement}_{hash}'

def set_mtime(path: Path, time: int):
    """utime wants to set atime and mtime, we just set it the same"""
    os.utime(path, (time, time))

def output_path_exists(path: Path, file: adsk.core.DataFile) -> bool:
    """
    Check if the file path already exists with version extension.
    Also checks for archived versions of the files to export and if update_existing_file_times
    is set, updates the mtime of existing files (not the archives).
    """
    if path.exists():
        if update_existing_file_times:
            set_mtime(path, file.dateModified)
            log(f'{path} already exists, but mtime was corrected')
        else:
            log(f'{path} already exists, skipping')
        return True

    for archive_extension in archive_extensions:
        archive_path = path.with_name(path.name + archive_extension)
        if archive_path.exists():
            log(f'{path} already exists as archive, skipping')
            return True

    return False

# component: adsk.core.Component but that doesn't exist for some reason?
# sketch   : adsk.core.Sketch likewise
def export_sketch(ctx: Ctx, doc: LazyDocument, component, sketch):
    output_path = ctx.folder / f'{sanitize_filename(sketch.name)}{VERSION_SEPARATOR}v{doc.file.versionNumber}.dxf'
    record_output(ctx, output_path)
    if output_path_exists(output_path, doc.file):
        return Counter(skipped=1)

    log(f'Exporting sketch {sketch.name} in {component.name} to {output_path}')
    output_path.parent.mkdir(exist_ok=True, parents=True)
    with atomic_write(output_path) as tmp_path:
        if not sketch.saveAsDXF(str(tmp_path)):
            raise RuntimeError(f'Fusion failed to export sketch `{sketch.name}` to {output_path}')
    set_mtime(output_path, doc.file.dateModified)
    return Counter(saved=1)

def visit_sketches(ctx: Ctx, doc: LazyDocument, component):
    counter = Counter()
    for sketch in component.sketches:
        try:
            counter += export_sketch(ctx, doc, component, sketch)
        except Exception:
            log(traceback.format_exc())
            counter.errored += 1

    for occurrence in component.occurrences:
        counter += visit_sketches(ctx.extend(sanitize_filename(occurrence.name)), doc, occurrence.component)

    return counter

def tree_gen(file: adsk.core.DataFile) -> Path:
    folders = []
    df = file
    while True:
        if not df.parentFolder:
            break
        folders.append(sanitize_filename(df.parentFolder.name))
        df = df.parentFolder

    folders.reverse()
    return Path(*folders)

def export_filename(ctx: Ctx, file: adsk.core.DataFile, format: Format=None, separator=None):
    extension = file.fileExtension if format is None else format.value
    sanitized = sanitize_filename(file.name)
    separator = VERSION_SEPARATOR if separator is None else separator
    name = f'{sanitized}{separator}v{file.versionNumber}.{extension}'
    return ctx.folder / name

def export_file(ctx: Ctx, format: Format, doc: LazyDocument) -> Counter:
    output_path = export_filename(ctx, doc.file, format)
    record_output(ctx, output_path)
    if output_path_exists(output_path, doc.file):
        return Counter(skipped=1)

    doc.open()

    design = doc.design
    em = design.exportManager

    output_path.parent.mkdir(exist_ok=True, parents=True)
    log(f'Exporting {format.value} for `{doc.file.name}` v{doc.file.versionNumber} to {output_path}')
    with atomic_write(output_path) as tmp_path:
        output_path_s = str(tmp_path)

        if format == Format.F3D:
            options = em.createFusionArchiveExportOptions(output_path_s)
        elif format == Format.STL:
            options = em.createSTLExportOptions(design.rootComponent, output_path_s)
        elif format == Format.TMF:
            options = em.createC3MFExportOptions(design.rootComponent, output_path_s)
        elif format == Format.STEP:
            options = em.createSTEPExportOptions(output_path_s)
        elif format == Format.IGES:
            options = em.createIGESExportOptions(output_path_s)
        elif format == Format.SAT:
            options = em.createSATExportOptions(output_path_s)
        elif format == Format.SMT:
            options = em.createSMTExportOptions(output_path_s)

        else:
            raise Exception(f'Got unknown export format {format}')

        # f3d already saves everything that is hidden and for the thumbnail to look nice, we don't want to unhide everything
        # Note that because unhiding is a mutation, the order of calls to export_file matters, but f3d will be first
        if ctx.unhide_all and format != Format.F3D:
            doc.unhide_all()

        if not em.execute(options):
            raise RuntimeError(f'Fusion failed to export {format.value} to {output_path}')
        validate_export_file(tmp_path)

        # Finish the thumbnail before publishing the archive. A crash or exception
        # here must not leave a final filename that the next run will skip.
        if format == Format.F3D:
            log(f'Creating thumbnail for `{doc.file.name}` v{doc.file.versionNumber}')
            thumb_b64 = design.rootComponent.createThumbnail(256, 256, 'PNG').getAsBase64String()
            with zipfile.ZipFile(tmp_path, 'a') as zf:
                with zf.open('FusionAssetName[Active]/Previews/small.png', 'w') as fh:
                    fh.write(base64.b64decode(thumb_b64))

    set_mtime(output_path, doc.file.dateModified)
    log(f'Saved {output_path}')

    return Counter(saved=1)

def export_drawing(ctx: Ctx, format: Format, doc: LazyDocument) -> Counter:
    output_path = export_filename(ctx, doc.file, format)
    record_output(ctx, output_path)
    if output_path_exists(output_path, doc.file):
        return Counter(skipped=1)

    doc.open()

    drawing = adsk.drawing.Drawing.cast(ctx.app.activeProduct)
    em: adsk.drawing.DrawingExportManager = drawing.exportManager

    output_path.parent.mkdir(exist_ok=True, parents=True)
    log(f'Exporting PDF for `{doc.file.name}` v{doc.file.versionNumber} to {output_path}')
    with atomic_write(output_path) as tmp_path:
        output_path_s = str(tmp_path)

        options = em.createPDFExportOptions(output_path_s)

        if not em.execute(options):
            raise RuntimeError(f'Fusion failed to export PDF to {output_path}')
    log(f'PDF created {output_path}')
    set_mtime(output_path, doc.file.dateModified)
    log(f'Saved {output_path}')

    return Counter(saved=1)


def visit_file(ctx: Ctx, file: adsk.core.DataFile) -> Counter:
    log(f'Visiting file {file.name} v{file.versionNumber}.{file.fileExtension}')

    counter = Counter()

    # all versions and formats of a model (and its sketches) go together in <folder>/<model name>/
    if ctx.subfolder_per_model:
        ctx = ctx.extend(sanitize_filename(file.name))

    if file.fileExtension != 'f3d' and file.fileExtension != 'f2d':
        if not ctx.export_non_design_files:
            log(f'Skipping non-design file {file.name} with extension {file.fileExtension}')
            counter.skipped += 1
            return counter

        log(f'file {file.name} has extension {file.fileExtension} attempting direct download')

        try:
            output_path = export_filename(ctx, file)
            record_output(ctx, output_path)
            if output_path_exists(output_path, file):
                counter.skipped += 1
                return counter

            output_path.parent.mkdir(exist_ok=True, parents=True)

            with atomic_write(output_path, allow_empty=True) as tmp_path:
                if not file.download(str(tmp_path), None):  # Synchronous download
                    raise RuntimeError(f'Fusion failed to download {output_path}')

            set_mtime(output_path, file.dateModified)
            log(f'Saved {output_path}')
            counter.saved += 1

        except Exception:
            counter.errored += 1
            log(traceback.format_exc())

        return counter

    with LazyDocument(ctx, file) as doc:

        if ctx.save_sketches and file.fileExtension != 'f2d':
            doc.open()
            counter += visit_sketches(ctx.extend(sanitize_filename(doc.rootComponent.name)), doc, doc.rootComponent)

        if file.fileExtension == 'f2d' and Format.PDF in ctx.formats:
            try:
                counter += export_drawing(ctx, Format.PDF, doc)
            except BatchLimitReached:
                raise
            except Exception:
                counter.errored += 1
                log(traceback.format_exc())

        elif file.fileExtension == 'f3d':
            for format in ctx.formats:
                if format == Format.PDF:
                    continue
                try:
                    counter += export_file(ctx, format, doc)
                except BatchLimitReached:
                    raise
                except Exception:
                    counter.errored += 1
                    log(traceback.format_exc())

    return counter

def file_versions(file: adsk.core.DataFile, num_versions):
    # file.versions (should) start with the current/latest version
    # we discovered that file.versions is actually sorted by the string of the versionNumber
    # so for something with 11 versions, we get [9, 8, 7, 6, 5, 4, 3, 2, 11, 10, 1]
    # but versionNumber does appear to always be an int so far, not sure where that error creeps in
    # so we just have to resort by int
    # it's possible this is not ideal for very large version counts if the swig layer is actually lazy
    # and so we force the iterator, but not sure, and idk how to avoid it and still get the versions in the
    # right order.
    if num_versions == 0:
        # only the latest version is wanted and that is `file` itself, so skip fetching the version list,
        # which is a request to the cloud for every file even when everything is already exported
        yield file
        return

    versions = sorted(file.versions, key=lambda x: x.versionNumber, reverse=True)

    if versions[0].versionNumber != file.versionNumber:
        raise Exception(f'Expected versions[0] to be current file version, but got {versions[0].versionNumber}')

    if num_versions == -1:
        versions = versions[1:]
    else:
        versions = versions[1:num_versions+1]

    yield file
    prev = file.versionNumber
    for v in versions:
        if prev - v.versionNumber != 1:
            raise Exception(f'Versions not contiguous! prev={prev} cur={v.versionNumber}')
        yield v
        prev = v.versionNumber

def visit_folder(ctx: Ctx, folder, recurse=True) -> Counter:
    log(f'Visiting folder {folder.name}')

    new_ctx = ctx.extend(sanitize_filename(folder.name))

    counter = Counter()

    for file in folder.dataFiles:
        try:
            for file_version in file_versions(file, ctx.num_versions):
                counter += visit_file(new_ctx, file_version)
        except (DocumentCloseError, BatchLimitReached):
            raise
        except Exception:
            log(f'Got exception visiting file\n{traceback.format_exc()}')
            counter.errored += 1

    if recurse:
        for sub_folder in folder.dataFolders:
            counter += visit_folder(new_ctx, sub_folder)

    return counter

def selected_folders(ctx):
    if ctx.use_active_folder:
        root_folder = ctx.app.data.activeFolder
        if root_folder is None:
            raise RuntimeError('No folder is open in the Data Panel')
        new_ctx = ctx.extend(tree_gen(root_folder))
        yield new_ctx, root_folder, True
    else:
        for project_id, folder_ids in ctx.projects_folders.items():
            project = ctx.app.data.dataProjects.itemById(project_id)
            if project is None:
                raise RuntimeError(f'Selected project is no longer available: {project_id}')
            if not folder_ids:  # empty filter visits everything, including saved settings
                yield ctx, project.rootFolder, True
            else:
                if project.rootFolder.id in folder_ids:
                    yield ctx, project.rootFolder, False
                for folder in project.rootFolder.dataFolders:
                    if folder.id in folder_ids:
                        yield ctx, folder, True

def catalog_folder(ctx, folder, root, separator, recurse=True):
    """Discover metadata and expected outputs without opening any documents."""
    ctx = ctx.extend(sanitize_filename(folder.name))
    log(f'Cataloging folder {folder.name}')
    for file in folder.dataFiles:
        try:
            extension = file.fileExtension
            if extension == 'f3d':
                formats = [f for f in ctx.formats if f != Format.PDF]
                sketches = ctx.save_sketches
            elif extension == 'f2d':
                formats = [Format.PDF] if Format.PDF in ctx.formats else []
                sketches = False
            elif ctx.export_non_design_files:
                formats, sketches = [None], False
            else:
                continue
            if not formats and not sketches:
                continue
            versions = file_versions(file, ctx.num_versions) if extension in ('f3d', 'f2d') else [file]
            for version in versions:
                output_ctx = ctx.extend(sanitize_filename(version.name)) if ctx.subfolder_per_model else ctx
                yield {
                    'file_id': version.id, 'version_id': version.versionId,
                    'name': version.name, 'version': version.versionNumber,
                    'extension': version.fileExtension, 'modified': version.dateModified,
                    'folder': str(ctx.folder.relative_to(root)),
                    'formats': [f.value for f in formats if f is not None],
                    'sketches': sketches, 'unhide_all': ctx.unhide_all,
                    'subfolder_per_model': ctx.subfolder_per_model, 'separator': separator,
                    'expected_outputs': [str(export_filename(output_ctx, version, f, separator).relative_to(root))
                                         for f in formats],
                }
        except Exception:
            log(f'Could not catalog {file.name}:\n{traceback.format_exc()}')
            yield None
    if recurse:
        for subfolder in folder.dataFolders:
            yield from catalog_folder(ctx, subfolder, root, separator)

def queue_options(ctx, separator):
    options = ctx.to_dict()
    options.pop('max_documents')
    options['folder'] = str(ctx.folder.resolve())
    options['separator'] = separator
    options['projects_folders'] = {k: sorted(v) for k, v in options['projects_folders'].items()}
    if ctx.use_active_folder:
        folder = ctx.app.data.activeFolder
        options['active_folder_id'] = folder.id if folder is not None else None
    return options

def catalog_inventory(ctx, catalog, separator):
    scan = uuid4().hex
    errors = 0
    with catalog.scan():
        position = 0
        for folder_ctx, folder, recurse in selected_folders(ctx):
            for payload in catalog_folder(folder_ctx, folder, ctx.folder, separator, recurse=recurse):
                if payload is None:
                    errors += 1
                    continue
                catalog.discover(payload, scan, position)
                position += 1
        catalog.save_scan(scan, queue_options(ctx, separator), errors)
    return scan, errors

def run_catalog(ctx, catalog, scan, scan_errors=0):
    batch = BackupBatch(catalog, ctx.max_documents)
    result = BackupResult(errored=scan_errors, catalog_path=str(catalog.path))
    result.skipped = catalog.completed_outputs(scan)
    for job in catalog.queue(scan):
        payload, key = job['payload'], job['key']
        batch.outputs = list(payload['expected_outputs'])
        catalog.start(key)
        try:
            file = ctx.app.data.findFileById(payload['version_id'])
            if file is None or file.id != payload['file_id'] or file.versionNumber != payload['version']:
                raise RuntimeError(f'Could not resolve exact version {payload["version_id"]}')
            if file.name != payload['name'] or file.fileExtension != payload['extension']:
                raise RuntimeError('The cloud file changed during cataloging; refresh the queue')
            job_ctx = ctx._replace(folder=ctx.folder / payload['folder'], batch=batch)
            count = visit_file(job_ctx, file)
            result += count
            if count.errored:
                catalog.fail(key, f'{count.errored} export errors; see {log_file}')
            elif not catalog.outputs_present(payload, batch.outputs):
                raise RuntimeError('Some cataloged output files are missing after export')
            else:
                # Includes the sketch paths discovered during this job. A zero-
                # sketch document is complete too, so it need not be reopened.
                catalog.finish(key, batch.outputs)
        except BatchLimitReached as exc:
            catalog.defer(key)
            result.stop_reason = str(exc)
            break
        except DocumentCloseError as exc:
            catalog.fail(key, str(exc))
            result.errored += 1
            result.stop_reason = str(exc)
            log(traceback.format_exc())
            break
        except Exception:
            error = traceback.format_exc()
            catalog.fail(key, error)
            result.errored += 1
            log(error)
    counts = catalog.counts(scan)
    result.total = counts['total']
    result.completed = counts['done']
    result.remaining = counts['total'] - counts['done']
    result.failed = counts['error']
    result.opened = batch.opened
    result.session_opens = catalog.session_opens(batch.session)
    return result

def main(ctx: Ctx) -> BackupResult:
    ctx = ctx._replace(folder=ctx.folder.resolve())
    if ctx.max_documents < 1:
        raise ValueError('The document limit must be at least 1')
    init_directory(ctx.folder)
    init_logging(ctx.folder)
    log(ctx.dumps())
    with BackupCatalog(ctx.folder) as catalog:
        log(f'Cataloging the selected files before exporting. Progress database: {catalog.path}')
        scan, errors = catalog_inventory(ctx, catalog, VERSION_SEPARATOR)
        counts = catalog.counts(scan)
        log(f'Queue: {counts["total"]} document versions, {counts["done"]} complete, '
            f'{counts["total"] - counts["done"]} remaining')
        return run_catalog(ctx, catalog, scan, errors)

def message_box_traceback():
    adsk.core.Application.get().userInterface.messageBox(traceback.format_exc())

class I(StrEnum):
    """UI input ids"""
    directory = 'directory'
    file_types = 'file_types'
    use_active_folder = 'use_active_folder'
    show_folders = 'show_folders'
    projects = 'projects'
    unhide_all = 'unhide_all'
    version_count = 'version_count'
    all_versions = 'all_versions'
    save_sketches = 'save_sketches'
    version_separator_is_space = 'version_separator_is_space'
    export_non_design_files = 'export_non_design_files'
    subfolder_per_model = 'subfolder_per_model'
    max_documents = 'max_documents'
    queue_status = 'queue_status'
    refresh_queue = 'refresh_queue'

def context_from_inputs(inputs):
    iv = partial(input_value, inputs)
    return Ctx(
        app=adsk.core.Application.get(), folder=Path(iv(I.directory)).resolve(),
        formats=[FormatFromName[x] for x in input_selected(inputs, I.file_types)],
        use_active_folder=iv(I.use_active_folder), projects_folders=make_projects_folders(inputs),
        unhide_all=iv(I.unhide_all), save_sketches=iv(I.save_sketches),
        num_versions=-1 if iv(I.all_versions) else iv(I.version_count),
        export_non_design_files=iv(I.export_non_design_files),
        subfolder_per_model=iv(I.subfolder_per_model), max_documents=iv(I.max_documents),
    )

def update_queue_display(inputs, refresh=False):
    status = inputs.itemById(I.queue_status)
    ctx = context_from_inputs(inputs)
    separator = ' ' if input_value(inputs, I.version_separator_is_space) else '_'
    path = ctx.folder / '.fusion-exporter.sqlite3'
    if not refresh and not path.is_file():
        status.text = 'Queue not cataloged. Click Refresh Queue to count the selected document versions.'
        return
    if refresh:
        init_directory(ctx.folder)
    with BackupCatalog(ctx.folder) as catalog:
        if refresh:
            scan, errors = catalog_inventory(ctx, catalog, separator)
            counts = catalog.counts(scan)
            counts['scan_errors'] = errors
        else:
            counts = catalog.preview(queue_options(ctx, separator))
        if counts is None:
            status.text = 'Options changed. Click Refresh Queue to update the count.'
            return
        remaining = counts['total'] - counts['done']
        session_opens = catalog.session_opens(process_session_id())
        status.text = (f'{counts["total"]} total | {counts["done"]} completed | {remaining} remaining\n'
                       f'{counts["error"]} failed | {session_opens}/{ctx.max_documents} document opens this Fusion session')
        if counts['scan_errors']:
            status.text += f'\nQueue incomplete: {counts["scan_errors"]} cataloging errors.'

def populate_data_projects_list(dropdown, show_folders=False, selected=None):
    app = adsk.core.Application.get()
    dropdown.listItems.clear()

    if selected is None:
        selected = []

    if show_folders:
        for project in app.data.dataProjects:
            for folder in itertools.chain([project.rootFolder], project.rootFolder.dataFolders):
                name = f'{project.name}/{folder.name}'
                project_folders_d[name] = (project.id, folder.id)
                dropdown.listItems.add(name, name in selected)
    else:
        for project in app.data.dataProjects:
            project_folders_d[project.name] = (project.id, None)
            dropdown.listItems.add(project.name, project.name in selected)

class ExporterCommandInputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
         try:
            inputs = args.inputs
            if inputs.itemById(I.queue_status) is None:
                return  # The command dialog is still being built.
            if args.input.id == I.refresh_queue:
                if args.input.value:
                    args.input.value = False
                    update_queue_display(inputs, refresh=True)
                return
            if args.input.id == I.all_versions:
                inputs.itemById(I.version_count).isEnabled = not args.input.value
            elif args.input.id == I.use_active_folder:
                inputs.itemById(I.projects).isEnabled = not args.input.value
                inputs.itemById(I.show_folders).isEnabled = not args.input.value
            elif args.input.id == I.show_folders:
                populate_data_projects_list(inputs.itemById(I.projects), args.input.value)
            if args.input.id not in (I.queue_status, I.refresh_queue):
                update_queue_display(inputs)

         except:
            message_box_traceback()

class ExporterCommandCreatedEventHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command

            # http://help.autodesk.com/view/fusion360/ENU/?guid=GUID-C1BF7FBF-6D35-4490-984B-11EB26232EAD
            cmd.isExecutedWhenPreEmpted = False

            onExecute = ExporterCommandExecuteHandler()
            onDestroy = ExporterCommandDestroyHandler()
            onInputChanged = ExporterCommandInputChangedHandler()
            cmd.execute.add(onExecute)
            cmd.destroy.add(onDestroy)
            cmd.inputChanged.add(onInputChanged)
            handlers.extend([onExecute, onDestroy, onInputChanged])

            inputs = cmd.commandInputs
            last_settings = load_last_settings()

            export_folder = last_settings.get(I.directory, str(Path.home() / 'Desktop/Fusion360Export'))
            inputs.addStringValueInput(I.directory, 'Directory', export_folder)

            drop = inputs.addDropDownCommandInput(I.file_types, 'Export Types', adsk.core.DropDownStyles.CheckBoxDropDownStyle)
            selected_formats = last_settings.get(I.file_types, DEFAULT_SELECTED_FORMATS)
            for format in Format:
                drop.listItems.add(format.value, format.value in selected_formats)

            use_active_folder = last_settings.get(I.use_active_folder, False)
            inputs.addBoolValueInput(I.use_active_folder, 'Download Open Folder', True, '', use_active_folder)

            #T addBoolValueInput(id, name, checkbox?, icon, default)
            show_folders = last_settings.get(I.show_folders, False)
            inputs.addBoolValueInput(I.show_folders, 'Show Project Folders', True, '', show_folders)
            inputs.itemById(I.show_folders).isEnabled = not use_active_folder

            drop = inputs.addDropDownCommandInput(I.projects, 'Export Projects', adsk.core.DropDownStyles.CheckBoxDropDownStyle)
            projects = last_settings.get(I.projects)
            populate_data_projects_list(drop, show_folders=show_folders, selected=projects)
            inputs.itemById(I.projects).isEnabled = not use_active_folder

            unhide_all = last_settings.get(I.unhide_all, True)
            inputs.addBoolValueInput(I.unhide_all, 'Unhide All Bodies', True, '', unhide_all)

            versions_group = inputs.addGroupCommandInput('group_versions', 'Versions')
            #T addIntegerSpinnerCommand(id, name, min, max, spinStep, initialValue)
            version_count = last_settings.get(I.version_count, 0)
            versions_group.children.addIntegerSpinnerCommandInput(I.version_count, 'Number of Previous Versions', 0, 2**16-1, 1, version_count)

            all_versions = last_settings.get(I.all_versions, False)
            versions_group.children.addBoolValueInput(I.all_versions, 'Save ALL Versions', True, '', all_versions)
            inputs.itemById(I.version_count).isEnabled = not all_versions

            save_sketches = last_settings.get(I.save_sketches, False)
            inputs.addBoolValueInput(I.save_sketches, 'Save Sketches as DXF', True, '', save_sketches)

            version_separator_is_space = last_settings.get(I.version_separator_is_space, VERSION_SEPARATOR == ' ')
            inputs.addBoolValueInput(I.version_separator_is_space, 'Version Separator is Space', True, '', version_separator_is_space)

            export_non_design_files = last_settings.get(I.export_non_design_files, False)
            inputs.addBoolValueInput(I.export_non_design_files, 'Export Non-Design Files', True, '', export_non_design_files)

            subfolder_per_model = last_settings.get(I.subfolder_per_model, False)
            inputs.addBoolValueInput(I.subfolder_per_model, 'Subfolder Per Model', True, '', subfolder_per_model)

            max_documents = last_settings.get(I.max_documents, 450)
            inputs.addIntegerSpinnerCommandInput(I.max_documents, 'Max Documents Per Fusion Session', 1, 10000, 1, max_documents)
            inputs.addTextBoxCommandInput(I.queue_status, 'Backup Queue (document versions)', '', 3, True)
            inputs.addBoolValueInput(I.refresh_queue, 'Refresh Queue', False, '', False)
            update_queue_display(inputs)
        except:
            message_box_traceback()

def finish_script():
    global pending_export
    pending_export = None
    adsk.terminate()

class ExporterCommandDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        global options_closed
        # Let the script's run function resume after this callback has returned.
        # Neither export nor script termination belongs in this command event.
        options_closed = True

# Dont use yield and don't copy list items, swig wants to delete things
def selected(inputs):
    return [it.name for it in inputs if it.isSelected]

def make_projects_folders(inputs):
    ret = defaultdict(set)
    for it in inputs.itemById(I.projects).listItems:
        if it.isSelected:
            project_id, folder_id = project_folders_d[it.name]
            if folder_id is None:  # whole project was selected
                ret[project_id] = []
            else:
                ret[project_id].add(folder_id)
    return ret

def run_main(ctx):
    global log_fh, log_file
    log_fh = None
    log_file = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        counter = main(ctx)
        if not counter.remaining and not counter.errored:
            outcome = 'Backup complete.'
        elif counter.stop_reason:
            outcome = f'Paused: {counter.stop_reason}.\nRestart Fusion and run Exporter again to continue.'
        else:
            outcome = 'The run ended with unfinished work. Failed items remain queued.'
        summary = '\n'.join((
            'Run finished.', outcome,
            f'Saved {counter.saved} files',
            f'Skipped {counter.skipped} files',
            f'Encountered {counter.errored} errors',
            f'Queue: {counter.completed}/{counter.total} completed; {counter.remaining} remaining ({counter.failed} failed)',
            f'Document opens: {counter.opened} this run; {counter.session_opens}/{ctx.max_documents} this Fusion session',
            f'Backup catalog: {counter.catalog_path}',
            f'Log file is at {log_file}'
        ))
        log(summary)
        ui.messageBox(summary, 'Exporter — Run finished')

    except:
        tb = traceback.format_exc()
        adsk.core.Application.get().userInterface.messageBox(
            f'Run finished with an error.\nLog file is at {log_file}\n{tb}', 'Exporter — Run finished')
        if log_fh is not None:
            log(f'Got top level exception\n{tb}')
    finally:
        if log_fh is not None:
            log_fh.close()
            log_fh = None

def input_value(inputs, name):
    return inputs.itemById(name).value

def input_selected(inputs, name):
    return selected(inputs.itemById(name).listItems)

class ExporterCommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        global VERSION_SEPARATOR, pending_export
        pending_export = None
        try:
            inputs = args.command.commandInputs
            iv = partial(input_value, inputs)
            isel = partial(input_selected, inputs)

            save_last_settings({
                I.directory: iv(I.directory),
                I.file_types: isel(I.file_types),
                I.use_active_folder : iv(I.use_active_folder),
                I.show_folders: iv(I.show_folders),
                I.projects: isel(I.projects),
                I.unhide_all: iv(I.unhide_all),
                I.save_sketches: iv(I.save_sketches),
                I.version_count: iv(I.version_count),
                I.all_versions: iv(I.all_versions),
                I.version_separator_is_space: iv(I.version_separator_is_space),
                I.export_non_design_files: iv(I.export_non_design_files),
                I.subfolder_per_model: iv(I.subfolder_per_model),
                I.max_documents: iv(I.max_documents),
            })

            VERSION_SEPARATOR = ' ' if iv(I.version_separator_is_space) else '_'

            ctx = context_from_inputs(inputs)
            # Only collect settings here. Opening/closing documents inside a
            # command event is unsupported; run resumes after destroy returns.
            pending_export = ctx
        except:
            message_box_traceback()

def run(context):
    global options_closed, pending_export
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        cmd_defs = ui.commandDefinitions
        pending_export = None
        options_closed = False

        CMD_DEF_ID = 'aconz2_Exporter'
        cmd_def = cmd_defs.itemById(CMD_DEF_ID)
        # This isn't how all the other demo scripts manage the lifecycle, but if we don't delete the old
        # command then we get double inputs when we run a second time
        if cmd_def:
            cmd_def.deleteMe()

        cmd_def = cmd_defs.addButtonDefinition(
            CMD_DEF_ID,
            'Export all the things',
            'Tooltip',
        )

        cmd_created = ExporterCommandCreatedEventHandler()
        cmd_def.commandCreated.add(cmd_created)
        handlers.append(cmd_created)

        adsk.autoTerminate(False)
        if not cmd_def.execute():
            raise RuntimeError('Could not open the export options command')

        # Pump events only while the options dialog is open. After doEvents
        # returns, destroy and the command's transaction teardown have unwound.
        # The batch itself runs normally on the main thread, outside callbacks.
        while not options_closed:
            adsk.doEvents()
            if not options_closed:
                time.sleep(0.01)

        ctx = pending_export
        pending_export = None
        if ctx is not None:
            if ui.activeCommand != 'SelectCommand':
                ui.commandDefinitions.itemById('SelectCommand').execute()
            run_main(ctx)
    except:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))
    finally:
        finish_script()
