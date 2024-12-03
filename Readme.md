This is a Fusion 360 Script to bulk export your files. Currently will export `f3d` files to `f3d`, `igs`, `stp`, `smt`, `sat`, `3mf` and `stl`. Can export drawings to `dxf`.

Currently seeking feedback in issue [23](https://github.com/aconz2/Fusion360Exporter/issues/23) on folder output structure.

# Installation

1) Download this repo and unzip it somewhere.
2) In Fusion, goto UTILITIES > ADD-INS > Scripts and Add-Ins (or just hit Shift+S)
   * UTILITIES was previously known as TOOLS
3) Next to "My Scripts", hit the green plus icon
4) Select the folder where you unzipped it
5) "Exporter" should now appear under "My Scripts"

or see the [offical docs](https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/How-to-install-an-ADD-IN-and-Script-in-Fusion-360.html) though they recommend copying the folder into the scripts directory and restarting which seems more complicated to me.

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
7) Version Separator Is Space: Controls which character `_` or ` ` (space) is used between the name and version (ie. `name_v42.stl` or `name v42.stl`). Defaults to the original `_` and checking this will use ` ` (space) to better match Fusion.

The last run's settings are loaded by default (if they exist). They are stored next to the `Exporter.py` file on your file system in a file called `last_settings.json`. In "My Scripts", you can right-click "Exporter" and then "Open file location" to get there. If you rename projects or folders you will have to reselect those projects.

# Projects/Folders

By default, selecting a project from `Export Projects` will go through every file in every folder recursively.

If you enable `Show Project Folders`, the `Export Projects` dropdown is populated with the top level folders (with an additional `<root>`) of each project. Selecting the `<root>` folder visits files in the project's root folder, but does not recurse. Selecting any other folder will visit all the files in that folder AND recurse into it.

# Versions

By default, only the latest version of each file will be exported. You can change this behavior to either
1) save all versions
2) save the previous `n` versions. (`n=0` corresponds to the default because `0` additional versions are saved)

# Operation

For each document in each selected project, it will check that there is a file named `<export directory>/<project name>/<document name><version separator><version name>.<file extension>` OR that file name with a few compressed/archived suffixes (see the code). If that file does not exist, it will open the document and do an export of it, then close it. If there are multiple formats to export, it will only open the document once. The exported file has it's `Date Modified` attribute (or `mtime`) set to the modified date (time) of the document (see File Time section for additional info).

For sketches, it will create a folder hiearchy like `<export directory>/<project name>/<component names ...>/<sketch name>.dxf`.

Since document names might have invalid filename characters, we attempt to replace them with spaces. In order to avoid a false collision, if any chars are replaced, the document name will have 8 hexchars of sha256 hash of the original utf-8 encoded document name. Eg `model 1/2 \ * ? <morechars> ||` would be saved as `model 1 2        morechars    _29a6fecc_v1.f3d`

In some ways this is an export and in others, it is more of a sync, since it won't re-export files that already exist and it skips opening documents it doesn't need to (with the caveat being we always have to open every document when exporting sketches).

It will create a log file at `<export_directory>/<timestamp>.txt` that should have some more info if things go wrong.

# File Time

Starting in version `20240813.1`, newly exported files have their `Date Modified` (or `mtime`) attribute set to the document's modified time. If you didn't want this you could replace the `set_mtime` function with a noop (ie just `pass`) or open an issue if you think this should be more configurable. However, by default it will not update the `mtime` for files that already exist which have their `mtime` corresponding to when the file was exported, not when the document was modified. If you wanted to do a one-time synchronize for these times, look at the top of source for the variable `update_existing_file_times`. I'm leaving it off by default to avoid pointlessly setting it on every run. And you would need to run the one-time synchronize with all the file formats and projects and versions etc selected that correspond to all the files you want updated.

Folders' `mtime` are not handled.

# Limitations + Known Issues

1) Not sure what other file types are out there (simulation data maybe? etc) but it only handles `.f3d` documents
2) Only visible bodies are included in exports to all file formats except `f3d`. Use the "Unhide All" option to unhide them before exporting
3) Image renders might cause an error. See [#4](https://github.com/aconz2/Fusion360Exporter/issues/4)
4) Cloud solves might cause an error. See [#3](https://github.com/aconz2/Fusion360Exporter/issues/3)

# Saved Settings

To easily run the same settings repeatedly, you can copy-paste the `Template` folder in `UserScripts` so that you have `UserScripts/YourScriptName/YourScriptName.{py,manifest}`. Then, open up a log file of a run that you want to replicate and copy paste the JSON blob at the beginning into `YourScriptName.py`. Then add this into Fusion as a script and run normally.

Note that we store project and folder id's, so renaming a project/folder will not break your backup script. But if you happen to replace the folder with a new one of the same name, it won't work.

You might run into an issue with the `VERSION_SEPARATOR` (whether it is export `file_v42.stl` or `file v42.stl`) if you are using saved settings.

# TODO (Maybe)

1) Saving electronics documents? these are `fbrd` files
2) Per-component export of `stl` (or other format); currently only the root component is exported with everything in it

# Credit

* Pulled the addition of `3mf` from [tavdog](https://github.com/tavdog/Fusion360Exporter)
* Problematic `"` in project names reported by TheShanMan
* Installation doc improvement reported by sqlBender
* Version ordering bug reported and diagnosed by loglow