This is a Fusion 360 Script to bulk export your files. Currently will export `f3d` files to `f3d`, `igs`, `stp`, `smt`, `sat`, `3mf` and `stl`. Can export drawings to `dxf`.

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
4) After selecting your options and hitting okay, **your computer will be unusable**. The script will be potentially opening and exporting a lot of documents and each time it opens one, Fusion likes to make itself the active window which means you can't just have this run in the background (as far as I know).

# Options

1) Directory: This defaults to a folder called Fusion360Exports on your desktop
2) File types: Select the export file types you want for each file
3) Projects: Select the projects (or folders) you want to operate on
    * Show Project Folders: Instead of selecting whole projects, you can select specific folders. See [Projects/Folders](#ProjectsFolders)
4) Unhide All: When checked, it will unhide all components and all bodies (recursively) so that the exported files contain all bodies
5) Export Sketches as DXF: Each sketch will get exported as dxf
6) Versions: Control how many versions are exported. See [Versions](#Versions)

# Projects/Folders

By default, selecting a project from `Export Projects` will go through every file in every folder recursively.

If you enable `Show Project Folders`, the `Export Projects` dropdown is populated with the top level folders (with an additional `<root>`) of each project. Selecting the `<root>` folder visits files in the project's root folder, but does not recurse. Selecting any other folder will visit all the files in that folder AND recurse into it.

# Versions

By default, only the latest version of each file will be exported. You can change this behavior to either
1) save all versions
2) save the previous `n` versions. (`n=0` corresponds to the default because `0` additional versions are saved)

# Operation

For each document in each selected project, it will ensure that there is a file named `<export directory>/<project name>/<document name>_<version name>.<file extension>`. If that file does not exist, it will open the document and do an export of it, then close it. If there are multiple formats to export, it will only open the document once.

For sketches, it will create a folder hiearchy like `<export directory>/<project name>/<component names ...>/<sketch name>.dxf`.

Since document names might have invalid filename characters, we attempt to replace them with spaces. In order to avoid a false collision, if any chars are replaced, the document name will have 8 hexchars of sha256 hash of the original utf-8 encoded document name. Eg `model 1/2 \ * ? <morechars> ||` would be saved as `model 1 2        morechars    _29a6fecc_v1.f3d`

In some ways this is an export and in others, it is more of a sync, since it won't re-export files that already exist and it skips opening documents it doesn't need to (with the caveat being we always have to open every document when exporting sketches).

It will create a log file at `<export_directory>/<timestamp>.txt` that should have some more info if things go wrong.

# Limitations + Known Issues

1) Not sure what other file types are out there (simulation data maybe? etc) but it only handles `.f3d` documents
2) Only visible bodies are included in exports to all file formats except `f3d`. Use the "Unhide All" option to unhide them before exporting
3) Image renders might cause an error. See [#4](https://github.com/aconz2/Fusion360Exporter/issues/4)
4) Cloud solves might cause an error. See [#3](https://github.com/aconz2/Fusion360Exporter/issues/3)

# Saved Settings

To easily run the same settings repeatedly, you can copy-paste the `Template` folder in `UserScripts` so that you have `UserScripts/YourScriptName/YourScriptName.{py,manifest}`. Then, open up a log file of a run that you want to replicate and copy paste the JSON blob at the beginning into `YourScriptName.py`. Then add this into Fusion as a script and run normally.

Note that we store project and folder id's, so renaming a project/folder will not break your backup script. But if you happen to replace the folder with a new one of the same name, it won't work.

# TODO (Maybe)

1) Saving electronics documents? these are `fbrd` files

# Credit

* Pulled the addition of `3mf` from [tavdog](https://github.com/tavdog/Fusion360Exporter)
* Problematic `"` in project names reported by TheShanMan
