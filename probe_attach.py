import time, json
from pathlib import Path
from zai_driver import connect, find_file_inputs

tmp = Path(__file__).resolve().parent / "sessions" / "_attach_test.txt"
tmp.write_text("hello attach probe", encoding="utf-8")
c = connect(9222)
ids = find_file_inputs(c)
print("FILE_INPUTS", len(ids), flush=True)
if ids:
    c.send("DOM.setFileInputFiles", {"files": [str(tmp)], "nodeId": ids[0]}, timeout=15)
    time.sleep(1.5)
    r = c.send("Runtime.evaluate",
               {"expression": "document.body.innerText.includes('_attach_test.txt')",
                "returnByValue": True}, timeout=10)
    print("CHIP_PRESENT", r.get("result", {}).get("value"), flush=True)
c.close()
tmp.unlink()
