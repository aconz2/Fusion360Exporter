This is a Fusion 360 Script to bulk export your files. Currently will export `f3d` files to `f3d`, `igs`, `stp`, `smt`, `sat` and `stl`.

# Installation

1) Download this repo and unzip it somewhere.
2) In Fusion, goto TOOLS > ADD-INS > Scripts and Add-Ins (or just hit Shift+S)
3) Next to "My Scripts", hit the green plus icon
4) Select the folder where you unzipped it
5) "Exporter" should now appear under "My Scripts"

# Usage

1) Goto Scripts and Add-Ins
2) Select Exporter from "My Scripts"
3) Hit Run. It will take a second to display the options panel as it is fetching your list of projects.
4) After selecting your options and hitting okay, **your computer will be unusable**. The script will be potentially opening and exporting a lot of documents and each time it opens one, Fusion likes to make itself the active window which means you can't just have this run in the background.

# Options

1) Directory: This defaults to a folder called Fusion360Exports on your desktop. Sorry about the teeny tiny text input size for this; I can't figure out how to resize it
2) File types: Select the export file types you want for each file
3) Projects: Select the projects you want to work on
4) Unhide All: When checked, it will unhide all components and all bodies (recursively) so that the exported files contain all bodies
5) Export Sketches as DXF: Each sketch will get exported as dxf

# Operation

For each document in each selected project, it will ensure that there is a file named `<export directory>/<project name>/<document name>_<version name>.<file extension>`. If that file does not exist, it will open the document and do an export of it, then close it. If there are multiple formats to export, it will only open the document once.

For sketches, it will create a folder hiearchy like `<export directory>/<project name>/<component names ...>/<sketch name>.dxf`.

Since document names might have invalid filename characters, we attempt to replace them with spaces. In order to avoid a false collision, if any chars are replaced, the document name will have 8 hexchars of sha256 hash of the original utf-8 encoded document name. Eg `model 1/2 \ * ? <morechars> ||` would be saved as `model 1 2        morechars    _29a6fecc_v1.f3d`

In some ways this is an export and in others, it is more of a sync, since it won't re-export files that already exist and it skips opening documents it doesn't need to.

It will create a log file at `<export_directory>/<timestamp>.txt` that should have some more info if things go wrong.

# Limitations

1) Not tested with drawings. I couldn't find anyting about exporting to DXF in the API docs
2) Not sure what other file types are out there (simulation data maybe? etc) but it only handles `.f3d` documents
3) Only visible bodies are included in exports to all file formats except `f3d`. Use the "Unhide All" option to unhide them before exporting