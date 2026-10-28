"""Copy tonight's board into site/, where a push deploys it.

The live site is a copy of what the chain just built: the template it was
built from and the data it was built with. Both are small, both are
plain files, and putting them under version control is what makes the
deploy -- Render redeploys on a push, so the nightly run ends by
publishing itself.

Two files, not the assembled page, because the page is 650 KB and the
data that changes nightly is the smaller half of it; committing the
whole thing every night would grow the history by a quarter of a
gigabyte a year for nothing.

    python publish.py            # copy into site/
    python publish.py --push     # and commit and push it

Nothing here touches the API key, and nothing in site/ can spend one.
"""
import json
import os
import shutil
import subprocess
import sys

DIR = os.path.dirname(os.path.abspath(__file__))
PAIRS = [(DIR + "/tool/template.html", DIR + "/site/template.html"),
         (DIR + "/out/tool_data.json", DIR + "/site/data.json")]


def copy():
    with open(PAIRS[1][0]) as fh:
        meta = json.load(fh).get("meta", {})
    os.makedirs(DIR + "/site", exist_ok=True)
    for src, dst in PAIRS:
        shutil.copy2(src, dst)
        print(f"  {os.path.basename(dst):16s} "
              f"{os.path.getsize(dst) / 1024:6.0f} KB")
    print(f"published slate {meta.get('slate')} built {meta.get('built')}")
    return meta


def push(meta):
    if not os.path.isdir(DIR + "/.git"):
        print("not a git repository -- nothing to push. See the README for "
              "the one-time setup.")
        return 1
    def git(*a):
        return subprocess.run(("git",) + a, cwd=DIR, capture_output=True,
                              text=True)
    if not git("diff", "--quiet", "--", "site").returncode == 0 \
            or git("ls-files", "--others", "--exclude-standard",
                   "site").stdout.strip():
        git("add", "site")
        msg = f"board for {meta.get('slate')} ({meta.get('built')})"
        r = git("commit", "-m", msg)
        if r.returncode:
            print("  commit:", (r.stdout + r.stderr).strip()[:200])
            return 1
        print(f"  committed: {msg}")
    else:
        print("  nothing changed")
        return 0
    r = git("push")
    print("  push:", (r.stdout + r.stderr).strip()[:300] or "ok")
    return r.returncode


if __name__ == "__main__":
    m = copy()
    sys.exit(push(m) if "--push" in sys.argv else 0)
