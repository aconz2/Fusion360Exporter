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

ctx = Exporter.Ctx(
    app=App(
        documents=Documents(),
    ),
    folder=Path('/tmp'),
    formats=[Exporter.Format.F3D, Exporter.Format.STEP],
    projects_folders={},
    unhide_all=True,
    save_sketches=True,
    num_versions=-1,
    export_non_design_files=True,
)
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

def run(ctx, folder):
    g_record_set_mtimes.reset()
    counter = Exporter.visit_folder(ctx, folder)
    saves = g_record_set_mtimes.saves
    return counter, saves

counter, saves = run(ctx, folder)
print('counter', counter)
for file in saves:
    print('saved', file)
