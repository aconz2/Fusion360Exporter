This is a Fusion 360 Script to bulk export your files. Can export:

* `f3d` files to `f3d`, `igs`, `stp`, `smt`, `sat`, `3mf` and `stl`
* `f2d` files to `pdf`
* can export drawings to `dxf`

# Installation

1) Download this repo and unzip it somewhere.
2) In Fusion, goto UTILITIES > ADD-INS > Scripts and Add-Ins (or just hit Shift+S)
   * UTILITIES was previously known as TOOLS
3) Next to "My Scripts", hit the green plus icon
4) Select the folder where you unzipped it
5) "Exporter" should now appear under "My Scripts"

or see the [offical docs](https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/How-to-install-an-ADD-IN-and-Script-in-Fusion-360.html) though they recommend copying the folder into the scripts directory and restarting which seems more complicated to me.

# Versions

Released versions are tagged with `YYYYMMDD.N` and can be found in the [tags page](https://github.com/aconz2/Fusion360Exporter/tags)

# Usage

1) Goto Scripts and Add-Ins
2) Select Exporter from "My Scripts"
3) Hit Run. It will take a second to display the options panel as it is fetching your list of projects.
4) Select your options and hit okay. Models are opened in the background without activating a document tab. Fusion still does the export work on its main thread, so it may be unresponsive during individual operations. Drawing PDF exports open and activate the drawing.

# Options

1) Directory: This defaults to a folder called Fusion360Exports on your desktop
2) File types: Select the export file types you want for each file. See [File Types](#FileTypes)
3) Download Open Folder: Uses the current folder in the Data Panel as the starting folder to initiate an export. See [Projects/Folders](#ProjectsFolders)
4) Projects: Select the projects (or folders) you want to operate on
    * Show Project Folders: Instead of selecting whole projects, you can select specific folders. See [Projects/Folders](#ProjectsFolders)
5) Unhide All: When checked, it will unhide all components and all bodies (recursively) so that the exported files contain all bodies
6) Export Sketches as DXF: Each sketch will get exported as dxf
7) Versions: Control how many versions are exported. See [Versions](#Versions)
8) Version Separator Is Space: Controls which character `_` or ` ` (space) is used between the name and version (ie. `name_v42.stl` or `name v42.stl`). Defaults to the original `_` and checking this will use ` ` (space) to better match Fusion.
9) Export Non-Design Files: If true, all [non-design files](https://help.autodesk.com/view/PLM/ENU/?guid=UG-ATTTAB-ATTACHMENTS) will be exported. Note that only the latest version will be exported and the version number will not be appended.
10) Subfolder Per Model: If true, each model's exports (all formats, versions and sketches) are saved in their own folder named after the model, ie. `<export directory>/<project name>/<document name>/<document name><version separator><version name>.<file extension>`. Files already exported without this option are not detected, so toggling it re-exports everything into the new layout.
11) Max Documents Per Fusion Session: Defaults to **450** document opens. Each saved version counts separately. The allowance persists across script invocations in the same Fusion process. After reaching it, restart Fusion and run Exporter again to continue the queue. The exporter can also pause earlier if Windows UI resource use is high.
12) Backup Queue: Shows total, completed, remaining, and failed document versions, plus this Fusion session's document-open count. Click **Refresh Queue** to catalog the current selection without opening models. Changing backup options requires refreshing the count.

The last run's settings are loaded by default (if they exist). They are stored next to the `Exporter.py` file on your file system in a file called `last_settings.json`. In "My Scripts", you can right-click "Exporter" and then "Open file location" to get there. If you rename projects or folders you will have to reselect those projects.

# File Types

To export `f2d` files as `pdf` (this is the only available option; they cannot be downloaded as f2d), select `PDF` for the export type.

All other file types apply to `f3d`.

# Projects/Folders

If `Download Open Folder` is selected, the files will be saved with all parent folders created to match the structure in Fusion. For example, if `A/B/C` is the active folder, then we export everything in and beneath folder `C` and those get saved on your computer as `<export directory>/A/B/C/<file name etc>`

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

The exporter catalogs selected files and versions before exporting, and stores the inventory and progress in `.fusion-exporter.sqlite3` in the export directory. Each document version is marked complete only after its selected exports and sketches succeed and its document closes. Completed documents are skipped without reopening them, including when exporting sketches. Missing outputs put the document back in the queue. Existing model exports from earlier runs are recognized; sketches need one successful cataloged run to record their complete list.

Every invocation refreshes the selected inventory to discover new files and versions. Completed work is retained when switching between latest-only and all-version backups. Failed and interrupted documents remain queued, with untouched pending work processed first. A crash can leave the current document unfinished; its already saved output files are skipped when it is retried.

Each run ends with a **Run finished** popup showing saved files, queue progress, and the reason for stopping. A limit stop says **Paused** and reports the remaining work. **Backup complete** appears only when every queued document version is complete and there were no cataloging or export errors. Canceling the options dialog does not start a run.

It will create a log file at `<export_directory>/<timestamp>.txt` that should have some more info if things go wrong.

The script processes Fusion events while the options dialog is open, then starts the batch after the options command finishes. Export work runs on Fusion's main thread outside the command callbacks, so document opening and closing happen outside the command transaction.

Models open with `visible=False` to reduce UI work during large exports. After each document closes, the script releases Python wrappers and processes Fusion events to allow deferred cleanup before opening the next document. On Windows, the log also records GDI and USER handle counts to help diagnose UI resource accumulation.

Exports use unique temporary filenames and are published to their final names only after the export succeeds and the output exists. F3D thumbnail updates are completed before publishing the archive. Failed exports are logged and can be retried; a failure to close a document stops the batch to avoid accumulating open documents. Logs record document opening, activation, export, thumbnail creation, and closure stages.

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
* Non-design file support added by [raphael-bmec-co](https://github.com/raphael-bmec-co)
* f2d support and using active folder by robertkuyper
* bug fix by hiroloquy
* Figured out f3d thumbnails with Happy1Snappy

# Dev

Run `python -B -m unittest -v test` for regression tests. The tests use temporary files and a stub Fusion API; they do not exercise Fusion's native code.

Discussion about some changes are in issue [23](https://github.com/aconz2/Fusion360Exporter/issues/23) on folder output structure. I implemented most of those ideas in the `dev` branch some time ago -- including a way to write tests -- but never felt like it was a great change so it is abandoned.

