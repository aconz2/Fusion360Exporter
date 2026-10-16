import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent))

from unittest.mock import Mock, MagicMock

sys.modules['adsk'] = Mock()
sys.modules['adsk.core'] = Mock()
sys.modules['adsk.drawing'] = Mock()
sys.modules['adsk.fusion'] = Mock()

from typing import List, Any
from dataclasses import dataclass
import tempfile
import zipfile

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
class ExportManager:
    """create*ExportOptions returns the output path and execute writes a file there, like Fusion does"""
    def __getattr__(self, name):
        if name.startswith('create') and name.endswith('ExportOptions'):
            return lambda *args: args[-1]
        raise AttributeError(name)

    def execute(self, path):
        # f3d is a zip and gets a thumbnail appended, so write a zip for every format
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('design', '')
        return True

@dataclass
class Design:
    rootComponent: Any
    exportManager: ExportManager

Exporter.design_from_document = lambda document: Design(rootComponent=document._file.rootComponent, exportManager=ExportManager())

@dataclass
class Documents:
    def open(self, file):
        return Document(_file=file)

    # no documents are already open
    def __iter__(self):
        return iter(())

@dataclass
class App:
    documents: Documents

@dataclass
class Sketch:
    name: str

    def saveAsDXF(self, path):
        Path(path).write_text(self.name)
        return True

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

    def createThumbnail(self, width, height, format):
        return Mock(getAsBase64String=Mock(return_value='aGk='))

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
    folder=Path(tempfile.mkdtemp()),
    formats=[Exporter.Format.F3D, Exporter.Format.STEP],
    projects_folders={},
    use_active_folder=False,
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

# 3 versions, each with 1 sketch, an f3d and a step
assert counter == Exporter.Counter(saved=9), counter
assert all(file.exists() for file in saves)
print('ok')
