import sys
from pathlib import Path
sys.path.append(Path(__file__).parent)

from unittest.mock import Mock, MagicMock

sys.modules['adsk'] = Mock()
sys.modules['adsk.core'] = Mock()

from typing import List, Any
from dataclasses import dataclass

import Exporter

Exporter.log = print
Exporter.init_directory = Mock()
Exporter.init_logging = Mock()
Exporter.output_path_exists = Mock(return_value=False)
Exporter.unhide_all_in_document = Mock()

class RecordSetMtimes:
    def __init__(self):
        self.saves = []

    def set_mtime(self, path, mtime):
        self.saves.append(path)

    def reset(self):
        self.saves.clear()

g_record_set_mtimes = RecordSetMtimes()
Exporter.set_mtime = g_record_set_mtimes.set_mtime

@property
def LazyDocument_rootComponent(self):
    return self._document._file.rootComponent

Exporter.LazyDocument.rootComponent = LazyDocument_rootComponent
# LazyDocument.design returns a mock because it calls adsk. ... .cast
# this is convenient b/c .exportManager gets mocked too

Path.mkdir = Mock()

@dataclass
class Documents:
    def open(self, file):
        return Document(_file=file)

@dataclass
class App:
    documents: Documents

@dataclass
class Sketch:
    name: str

    def saveAsDXF(self, path):
        pass

@dataclass
class Component:
    name: str
    sketches: List[Sketch]
    components: List['Component'] = ()

    # I'm not confident about how occurrences actually works, I thought it was
    # the sub-components but not entirely sure
    @property
    def occurrences(self):
        return [Occurrence(component=c) for c in self.components]

@dataclass
class Occurrence:
    component: Component

    @property
    def name(self):
        return self.component.name

@dataclass
class File:
    name: str
    versionNumber: int
    fileExtension: str
    rootComponent: Component

    def with_version(self, version: int):
        return File(
            name=self.name,
            fileExtension=self.fileExtension,
            rootComponent=self.rootComponent,
            versionNumber=version,
        )

    def close(self, arg):
        pass

    @property
    def versions(self):
        return [self.with_version(i) for i in range(self.versionNumber, 0, -1)]

    @property
    def dateModified(self):
        None

@dataclass
class Document:
    _file: File

    def close(self, save_changes):
        pass

    def activate(self):
        pass

    @property
    def name(self):
        self._file.name

@dataclass
class Folder:
    name: str
    dataFiles: List[File]
    dataFolders: List['Folder']

def default_config(**kwargs):
    d = dict(
        folder=Path('/out'),
        formats=[Exporter.Format.F3D, Exporter.Format.STEP],
        projects_folders={},
        unhide_all=True,
        save_sketches=True,
        num_versions=-1,
        export_non_design_files=True,
        filepath_file_formatter=Exporter.FilepathFormatter('{project}/{folders:sep=/}/{file} v{version}.{ext}'),
        filepath_component_formatter=Exporter.FilepathFormatter('{project}/{folders:sep=/}/{file}/{components:sep=_}/{name} v{version}.{ext}'),
        per_component=True,
        force=False,
    )
    d = {**d, **kwargs}
    return Exporter.Config(**d)

def default_ctx(config, project_name='project1'):
    app = App(
        documents=Documents(),
    )
    return Exporter.Ctx.new(config, project_name, app)

def run(ctx, folder):
    g_record_set_mtimes.reset()
    counter = Exporter.visit_folder(ctx, folder)
    saves = g_record_set_mtimes.saves
    return counter, saves

ANY_FAILED = False

def test(ctx, folder, expected):
    counter, saves = run(ctx, folder)
    expected = set(expected)
    saves = set(map(str, saves))
    if expected == saves:
        print('PASS')
        return
    expected_not_saved = sorted(expected - saves)
    if expected_not_saved:
        print('--- expected but was not saved ---')
    for x in expected_not_saved:
        print(f'{x!r},')
    saved_not_expected = sorted(saves - expected)
    if saved_not_expected:
        print('-- saved but was not expected ---')
    for x in saved_not_expected:
        print(f'{x!r},')
    global ANY_FAILED
    any_failed = True

file1 = File(
    name='file1',
    fileExtension='f3d',
    versionNumber=3,
    rootComponent=Component(
        name='component1',
        sketches=[
            Sketch(
                name='sketch1',
            ),
        ],
        components=[
            Component(
                name='component1a',
                sketches=[],
            ),
        ],
    ),
)
folder = Folder(
    name='folder1',
    dataFiles=[
        file1,
    ],
    dataFolders=[],
)

ctx = default_ctx(default_config())
test(ctx, folder, [
    '/out/project1/folder1/file1/component1/component1 v1.f3d',
    '/out/project1/folder1/file1/component1/component1 v1.step',
    '/out/project1/folder1/file1/component1/component1 v2.f3d',
    '/out/project1/folder1/file1/component1/component1 v2.step',
    '/out/project1/folder1/file1/component1/component1 v3.f3d',
    '/out/project1/folder1/file1/component1/component1 v3.step',
    '/out/project1/folder1/file1/component1/sketch1 v1.dxf',
    '/out/project1/folder1/file1/component1/sketch1 v2.dxf',
    '/out/project1/folder1/file1/component1/sketch1 v3.dxf',
    '/out/project1/folder1/file1/component1_component1a/component1a v1.f3d',
    '/out/project1/folder1/file1/component1_component1a/component1a v1.step',
    '/out/project1/folder1/file1/component1_component1a/component1a v2.f3d',
    '/out/project1/folder1/file1/component1_component1a/component1a v2.step',
    '/out/project1/folder1/file1/component1_component1a/component1a v3.f3d',
    '/out/project1/folder1/file1/component1_component1a/component1a v3.step',
])

ctx = default_ctx(default_config(per_component=False))
test(ctx, folder, [
    '/out/project1/folder1/file1 v1.f3d',
    '/out/project1/folder1/file1 v1.step',
    '/out/project1/folder1/file1 v2.f3d',
    '/out/project1/folder1/file1 v2.step',
    '/out/project1/folder1/file1 v3.f3d',
    '/out/project1/folder1/file1 v3.step',
    '/out/project1/folder1/file1/component1/sketch1 v1.dxf',
    '/out/project1/folder1/file1/component1/sketch1 v2.dxf',
    '/out/project1/folder1/file1/component1/sketch1 v3.dxf',
])

ctx = default_ctx(default_config(per_component=False, save_sketches=False))
test(ctx, folder, [
    '/out/project1/folder1/file1 v1.f3d',
    '/out/project1/folder1/file1 v1.step',
    '/out/project1/folder1/file1 v2.f3d',
    '/out/project1/folder1/file1 v2.step',
    '/out/project1/folder1/file1 v3.f3d',
    '/out/project1/folder1/file1 v3.step',
])

ctx = default_ctx(default_config(per_component=False, save_sketches=False, num_versions=0))
test(ctx, folder, [
    '/out/project1/folder1/file1 v3.f3d',
    '/out/project1/folder1/file1 v3.step',
])

f = Exporter.FilepathFormatter('{project}/{folders:sep=/}/{components:sep=_}/{file} v{version}.{ext}')
assert f.format(project='p1', folders=('f1', 'f2'), components=('c1', 'c2'), file='file', version=2, ext='stl') == 'p1/f1/f2/c1_c2/file v2.stl'
assert f.format(project='p1', folders=('f1', 'f2'), components=(), file='file', version=2, ext='stl') == 'p1/f1/f2//file v2.stl'

if ANY_FAILED:
    sys.exit(1)
