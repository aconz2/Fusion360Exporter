import adsk.core
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
from functools import partial

# If you have a bunch of files already existing and really want the files' date-modified attr
# to be correct but don't want to rerun an export, you can change this to True for a single run, then
# probably change it back so you're not spamming the pointless attr change every time
update_existing_file_times = False

# Older versions of this script used '_' as seperator but Fusion 360 uses ' ' per default in manual exports.
VERSION_SEPARATOR = '_' # use either ' ' or '_'

log_file = None
log_fh = None

handlers = []
# map from presentation of `project/folder` shown in UI to (project id, folder id)
# and also from `project` to (project id, None) if subfolders not enabled
# this is kinda hacky but not sure how reliable keying on the list item itself is
project_folders_d = {} # {f'{project.name}/{folder.name}': (project.id, folder.id)}

last_settings_path = Path(__file__).parent / 'last_settings.json'

def log(*args):
    print(*args, file=log_fh)
    log_fh.flush()

def init_directory(name):
    directory = Path(name)
    directory.mkdir(exist_ok=True)
    return directory

def init_logging(directory):
    global log_file, log_fh
    log_file = directory / '{:%Y_%m_%d_%H_%M}.txt'.format(datetime.now())
    log_fh = open(log_file, 'w', encoding="utf-8")

def load_last_settings():
    if not last_settings_path.exists():
        return {}
    with open(last_settings_path) as fh:
        return json.load(fh)

def save_last_settings(d):
    with open(last_settings_path, 'w') as fh:
        json.dump(d, fh, indent=2)

class Format(Enum):
    F3D = 'f3d'
    STEP = 'step'
    STL = 'stl'
    IGES = 'igs'
    SAT = 'sat'
    SMT = 'smt'
    TMF = '3mf'

FormatFromName = {x.value: x for x in Format}

DEFAULT_SELECTED_FORMATS = {Format.F3D.value, Format.STEP.value}

archive_extensions = ['.zip', '.rar', '.gz', '.tar.gz', '.tar.bz2', '.tar.xz']

class Ctx(NamedTuple):
    app: adsk.core.Application
    folder: Path
    formats: List[Format]
    projects_folders: Dict[str, List[str]] # {projectId: [folderId+]} empty list is taken to mean "no filter"
    unhide_all: bool
    save_sketches: bool
    num_versions: int # -1 means all versions

    def extend(self, other):
        return self._replace(folder=self.folder / other)

    def to_dict(self):
        d = self._asdict()
        d.pop('app')
        d['folder'] = str(d['folder'])
        d['formats'] = [x.value for x in d['formats']]
        d['projects_folders'] = {k: list(v) for k, v in d['projects_folders'].items()}
        return d

    def dumps(self):
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, d, app):
        d['app'] = app
        d['folder'] = Path(d['folder'])
        d['formats'] = [FormatFromName[x] for x in d['formats']]
        d['projects_folders'] = {k: set(v) for k, v in d['projects_folders'].items()}
        return cls(**d)

    def has_show_folders(self):
        return any(len(v) > 0 for v in self.projects_folders.values())

class LazyDocument:
    def __init__(self, ctx: Ctx, file: adsk.core.DataFile):
        self._ctx = ctx
        self._document = None
        self.file = file

    def open(self):
        if self._document is not None:
            return
        log(f'Opening `{self.file.name}`')
        self._document = self._ctx.app.documents.open(self.file)
        self._document.activate()

        if self._ctx.unhide_all:
            unhide_all_in_document(self._document)

    def close(self):
        if self._document is None:
            return
        log(f'Closing {self.file.name}')
        self._document.close(False)  # don't save changes

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

def design_from_document(document: adsk.core.Document):
    return adsk.fusion.FusionDocument.cast(document).design

def unhide_all_in_document(document: adsk.core.Document):
    unhide_all_in_component(design_from_document(document).rootComponent)

def unhide_all_in_component(component):
    component.isBodiesFolderLightBulbOn = True
    component.isSketchFolderLightBulbOn = True

    for brep in component.bRepBodies:
        brep.isLightBulbOn = True

    for body in component.meshBodies:
        body.isLightBulbOn = True

    # I find the name occurrences very confusing, but apparently that is what a sub-component is called
    for occurrence in component.occurrences:
        occurrence.isLightBulbOn = True
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

def output_path_exists(path: Path, doc: LazyDocument) -> bool:
    """
    Check if the file path already exists with version extension.
    Also checks for archived versions of the files to export and if update_existing_file_times
    is set, updates the mtime of existing files (not the archives).
    """
    if path.exists():
        if update_existing_file_times:
            set_mtime(path, doc.file.dateModified)
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
    output_path = ctx.folder / f'{sanitize_filename(sketch.name)}.dxf'
    if output_path_exists(output_path, doc):
        return Counter(skipped=1)

    log(f'Exporting sketch {sketch.name} in {component.name} to {output_path}')
    output_path.parent.mkdir(exist_ok=True, parents=True)
    sketch.saveAsDXF(str(output_path))
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

def export_filename(ctx: Ctx, format: Format, file: adsk.core.DataFile):
    sanitized = sanitize_filename(file.name)
    name = f'{sanitized}{VERSION_SEPARATOR}v{file.versionNumber}.{format.value}'
    return ctx.folder / name

def export_file(ctx: Ctx, format: Format, doc: LazyDocument) -> Counter:
    output_path = export_filename(ctx, format, doc.file)
    if output_path_exists(output_path, doc):
        return Counter(skipped=1)

    doc.open()

    # I'm just taking this from here https://github.com/tapnair/apper/blob/master/apper/Fusion360Utilities.py
    # is there a nicer way to do this??
    design = doc.design
    em = design.exportManager

    output_path.parent.mkdir(exist_ok=True, parents=True)
    output_path_s = str(output_path)

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

    em.execute(options)
    set_mtime(output_path, doc.file.dateModified)
    log(f'Saved {output_path}')

    return Counter(saved=1)

def visit_file(ctx: Ctx, file: adsk.core.DataFile) -> Counter:
    log(f'Visiting file {file.name} v{file.versionNumber}.{file.fileExtension}')

    if file.fileExtension != 'f3d':
        log(f'file {file.name} has extension {file.fileExtension} which is not currently handled, skipping')
        return Counter(skipped=1)

    with LazyDocument(ctx, file) as doc:
        counter = Counter()

        if ctx.save_sketches:
            doc.open()
            counter += visit_sketches(ctx.extend(sanitize_filename(doc.rootComponent.name)), doc, doc.rootComponent)

        for format in ctx.formats:
            try:
                counter += export_file(ctx, format, doc)
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
        except Exception:
            log(f'Got exception visiting file\n{traceback.format_exc()}')
            counter.errored += 1

    if recurse:
        for sub_folder in folder.dataFolders:
            counter += visit_folder(new_ctx, sub_folder)

    return counter

def main(ctx: Ctx) -> Counter:
    init_directory(ctx.folder)
    init_logging(ctx.folder)

    log(ctx.dumps())

    counter = Counter()

    for project_id, folder_ids in ctx.projects_folders.items():
        project = ctx.app.data.dataProjects.itemById(project_id)

        if folder_ids == []:  # empty filter visit everything
            counter += visit_folder(ctx, project.rootFolder)

        # if the root folder is the only thing selected, we take that to mean no recurse
        elif folder_ids == [project.rootFolder.id]:
            counter += visit_folder(ctx, project.rootFolder, recurse=False)

        else:
            folders = project.rootFolder.dataFolders
            # hmm this doesn't work, the itemsById doesn't return the folder
            # for folder_id in folder_ids:
            #     counter += visit_folder(ctx, folders.itemById(folder_id))
            for folder in filter(lambda x: x.id in folder_ids, folders):
                counter += visit_folder(ctx, folder)

    return counter

def message_box_traceback():
    adsk.core.Application.get().userInterface.messageBox(traceback.format_exc())

class I(StrEnum):
    """UI input ids"""
    directory = 'directory'
    file_types = 'file_types'
    show_folders = 'show_folders'
    projects = 'projects'
    unhide_all = 'unhide_all'
    version_count = 'version_count'
    all_versions = 'all_versions'
    save_sketches = 'save_sketches'
    version_separator_is_space = 'version_separator_is_space'

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
            if args.input.id == I.all_versions:
                inputs.itemById(I.version_count).isEnabled = not args.input.value
            elif args.input.id == I.show_folders:
                populate_data_projects_list(inputs.itemById(I.projects), args.input.value)

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

            #T addBoolValueInput(id, name, checkbox?, icon, default)
            show_folders = last_settings.get(I.show_folders, False)
            inputs.addBoolValueInput(I.show_folders, 'Show Project Folders', True, '', show_folders)
            
            drop = inputs.addDropDownCommandInput(I.projects, 'Export Projects', adsk.core.DropDownStyles.CheckBoxDropDownStyle)
            projects = last_settings.get(I.projects)
            populate_data_projects_list(drop, show_folders=show_folders, selected=projects)

            unhide_all = last_settings.get(I.unhide_all, True)
            inputs.addBoolValueInput(I.unhide_all, 'Unhide All Bodies', True, '', unhide_all)

            versions_group = inputs.addGroupCommandInput('group_versions', 'Versions')
            #T addIntegerSpinnerCommand(id, name, min, max, spinStep, initialValue)
            version_count = last_settings.get(I.version_count, 0)
            versions_group.children.addIntegerSpinnerCommandInput(I.version_count, 'Number of Previous Versions', 0, 2**16-1, 1, version_count)
            
            all_versions = last_settings.get(I.all_versions, False)
            versions_group.children.addBoolValueInput(I.all_versions, 'Save ALL Versions', True, '', all_versions)
            
            save_sketches = last_settings.get(I.save_sketches, False)
            inputs.addBoolValueInput(I.save_sketches, 'Save Sketches as DXF', True, '', save_sketches)

            version_separator_is_space = last_settings.get(I.version_separator_is_space, VERSION_SEPARATOR == ' ')
            inputs.addBoolValueInput(I.version_separator_is_space, 'Version Separator is Space', True, '', version_separator_is_space)
        except:
            message_box_traceback()

class ExporterCommandDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            adsk.terminate()
        except:
            message_box_traceback()

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
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        counter = main(ctx)
        ui.messageBox('\n'.join((
            f'Saved {counter.saved} files',
            f'Skipped {counter.skipped} files',
            f'Encountered {counter.errored} errors',
            f'Log file is at {log_file}'
        )))

    except:
        tb = traceback.format_exc()
        adsk.core.Application.get().userInterface.messageBox(f'Log file is at {log_file}\n{tb}')
        if log_fh is not None:
            log(f'Got top level exception\n{tb}')
    finally:
        if log_fh is not None:
            log_fh.close()

def input_value(inputs, name):
    return inputs.itemById(name).value

def input_selected(inputs, name):
    return selected(inputs.itemById(name).listItems)

class ExporterCommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            iv = partial(input_value, inputs)
            isel = partial(input_selected, inputs)

            save_last_settings({
                I.directory: iv(I.directory),
                I.file_types: isel(I.file_types),
                I.show_folders: iv(I.show_folders),
                I.projects: isel(I.projects),
                I.unhide_all: iv(I.unhide_all),
                I.save_sketches: iv(I.save_sketches),
                I.version_count: iv(I.version_count),
                I.all_versions: iv(I.all_versions),
                I.version_separator_is_space: iv(I.version_separator_is_space),
            })

            # kinda hacky
            if iv(I.version_separator_is_space):
                global VERSION_SEPARATOR
                VERSION_SEPARATOR = ' '

            ctx = Ctx(
                app = adsk.core.Application.get(),
                folder = Path(iv(I.directory)),
                formats = [FormatFromName[x] for x in isel(I.file_types)],
                projects_folders = make_projects_folders(inputs),
                unhide_all = iv(I.unhide_all),
                save_sketches = iv(I.save_sketches),
                num_versions = -1 if iv(I.all_versions) else iv(I.version_count),
            )
            run_main(ctx)
        except:
            message_box_traceback()

def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        cmd_defs = ui.commandDefinitions

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

        cmd_def.execute()

        adsk.autoTerminate(False)
    except:
        if ui:
            ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))
