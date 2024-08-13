import adsk.core
import traceback
from pathlib import Path
from datetime import datetime
from typing import NamedTuple, List, Set, Dict
from enum import Enum
from dataclasses import dataclass
import hashlib
import re
from collections import defaultdict
import itertools
import json
import os

# If you have a bunch of files already existing and really want the files' date-modified attr
# to be correct but don't want to rerun an export, you can change this to True for a single run, then
# probably change it back so you're not spamming the pointless attr change every time
update_existing_file_times = False

log_file = None
log_fh = None

handlers = []
# map from presentation of `project/folder` shown in UI to (project id, folder id)
# and also from `project` to (project id, None) if subfolders not enabled
# this is kinda hacky but not sure how reliable keying on the list item itself is
project_folders_d = {} # {f'{project.name}/{folder.name}': (project.id, folder.id)}

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
    log_fh = open(log_file, 'w')

class Format(Enum):
    F3D = 'f3d'
    STEP = 'step'
    STL = 'stl'
    IGES = 'igs'
    SAT = 'sat'
    SMT = 'smt'
    TMF = '3mf'

FormatFromName = {x.value: x for x in Format}

DEFAULT_SELECTED_FORMATS = {Format.F3D, Format.STEP}

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

# component: adsk.core.Component but that doesn't exist for some reason?
# sketch   : adsk.core.Sketch likewise
def export_sketch(ctx: Ctx, doc: LazyDocument, component, sketch):
    output_path = ctx.folder / f'{sanitize_filename(sketch.name)}.dxf'
    if output_path.exists():
        if update_existing_file_times:
            set_mtime(output_path, doc.file.dateModified)
        log(f'{output_path} already exists, skipping')
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
    name = f'{sanitized}_v{file.versionNumber}.{format.value}'
    return ctx.folder / name

def export_file(ctx: Ctx, format: Format, doc: LazyDocument) -> Counter:
    output_path = export_filename(ctx, format, doc.file)
    if output_path.exists():
        if update_existing_file_times:
            set_mtime(output_path, doc.file.dateModified)
        log(f'{output_path} already exists, skipping')
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
    log(f'Visiting file {file.name} v{file.versionNumber} . {file.fileExtension}')

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
    # file.versions starts with the current/latest version
    # I'm paranoid the versions won't always be contiguous so we check
    if num_versions == -1:
        versions = list(file.versions)[1:]
    else:
        versions = list(file.versions)[1:num_versions+1]

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

def populate_data_projects_list(dropdown, show_folders=False):
    app = adsk.core.Application.get()
    dropdown.listItems.clear()

    if show_folders:
        for project in app.data.dataProjects:
            for folder in itertools.chain([project.rootFolder], project.rootFolder.dataFolders):
                name = f'{project.name}/{folder.name}'
                project_folders_d[name] = (project.id, folder.id)
                dropdown.listItems.add(name, False)
    else:
        for project in app.data.dataProjects:
            project_folders_d[project.name] = (project.id, None)
            dropdown.listItems.add(project.name, False)

class ExporterCommandInputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
         try:
            inputs = args.inputs
            if args.input.id == 'all_versions':
                inputs.itemById('version_count').isEnabled = not args.input.value
            elif args.input.id == 'show_folders':
                populate_data_projects_list(inputs.itemById('projects'), args.input.value)

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

            inputs.addStringValueInput('directory', 'Directory', str(Path.home() / 'Desktop/Fusion360Export'))

            drop = inputs.addDropDownCommandInput('file_types', 'Export Types', adsk.core.DropDownStyles.CheckBoxDropDownStyle)
            for format in Format:
                drop.listItems.add(format.value, format in DEFAULT_SELECTED_FORMATS)

            #T addBoolValueInput(id, name, checkbox?, icon, default)
            inputs.addBoolValueInput('show_folders', 'Show Project Folders', True, '', False)
            drop = inputs.addDropDownCommandInput('projects', 'Export Projects', adsk.core.DropDownStyles.CheckBoxDropDownStyle)
            populate_data_projects_list(drop)

            inputs.addBoolValueInput('unhide_all', 'Unhide All Bodies', True, '', True)
            versions_group = inputs.addGroupCommandInput('group_versions', 'Versions')
            versions_group.children.addIntegerSpinnerCommandInput('version_count', 'Number of Previous Versions', 0, 2**16-1, 1, 0)
            versions_group.children.addBoolValueInput('all_versions', 'Save ALL Versions', True, '', False)
            inputs.addBoolValueInput('save_sketches', 'Save Sketches as DXF', True, '', False)
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
    for it in inputs.itemById('projects').listItems:
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

class ExporterCommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            app = adsk.core.Application.get()
            ctx = Ctx(
                app = app,
                folder = Path(inputs.itemById('directory').value),
                formats = [FormatFromName[x] for x in selected(inputs.itemById('file_types').listItems)],
                projects_folders = make_projects_folders(inputs),
                unhide_all = inputs.itemById('unhide_all').value,
                save_sketches = inputs.itemById('save_sketches').value,
                num_versions = -1 if inputs.itemById('all_versions').value else inputs.itemById('version_count').value,
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
