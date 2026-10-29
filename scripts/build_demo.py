"""Build the read-only demo as one self-contained HTML page (run snapshot_demo.py first)."""
import re
import subprocess
import sys
from pathlib import Path

web = Path("web")
out_dir = web / "dist-demo"
subprocess.run(["npx", "vite", "build", "--outDir", "dist-demo", "--emptyOutDir"], cwd=web, check=True,
               env={**__import__("os").environ, "VITE_STATIC": "1"})
html = (out_dir / "index.html").read_text()
css = "".join(p.read_text() for p in (out_dir / "assets").glob("*.css"))
js = "".join(p.read_text() for p in (out_dir / "assets").glob("*.js")).replace("</script", "<\\/script")
fonts = re.search(r'<link href="(https://fonts.googleapis.com[^"]+)" rel="stylesheet" />', html).group(1)
page = f"""<title>TenderFit Demo</title>
<meta name="description" content="Read-only demo of TenderFit, a supplier-side assistant for UK public tenders.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="{fonts}">
<style>:root{{color-scheme:light}}{css}</style>
<div id="root"></div>
<script type="module">{js}</script>
"""
target = Path(sys.argv[1]) if len(sys.argv) > 1 else out_dir / "tenderfit-demo.html"
target.write_text(page)
print(f"{target} {len(page) // 1024} KB")
