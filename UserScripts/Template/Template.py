from pathlib import Path
import shutil
import adsk.core
import traceback
import json
import sys

# fill between the ''' with the saved settings which is output at the beginning of every log file
data = json.loads(r'''
{
  "folder": "C:\\...",
  "formats": [
    "F3D",
    "STEP"
  ],
  "projects_folders": {
    "big long id": ["urn:adsk...."]
  },
  "unhide_all": true,
  "save_sketches": false,
  "num_versions": 1
}
''')

def run(context):
    try:
        d = str(Path(__file__).parent.parent.parent)
        if d not in sys.path:
            sys.path.append(d)
        from Exporter import run_main, Ctx
        run_main(Ctx.from_dict(data, adsk.core.Application.get()))
    except:
        adsk.core.Application.get().userInterface.messageBox(traceback.format_exc())