"""MkDocs hook: point links that leave docs/ at the repository on GitHub.

The guides link to scripts, raw results and the README with repo-relative paths,
which work on GitHub but not on the built site. Rewrite those to
<repo_url>/blob/main/<path> (or /tree/main/ for directories) so `mkdocs build
--strict` stays strict for links that really are broken.
"""

import os
import re

LINK = re.compile(r"(\]\()([^)\s#]+)(#[^)\s]*)?(\))")


def on_page_markdown(markdown, page, config, files):
    docs_dir = os.path.abspath(config["docs_dir"])
    root = os.path.dirname(docs_dir)
    repo = config.get("repo_url", "").rstrip("/")
    page_dir = os.path.dirname(os.path.join(docs_dir, page.file.src_path))

    def fix(m):
        target = m.group(2)
        if re.match(r"^[a-z]+:", target) or target.startswith("/"):
            return m.group(0)
        path = os.path.normpath(os.path.join(page_dir, target))
        inside = path == docs_dir or path.startswith(docs_dir + os.sep)
        # .py files are excluded from the site, so they link to the repository instead.
        if inside and not os.path.isdir(path) and not path.endswith(".py"):
            return m.group(0)
        if not path.startswith(root + os.sep) or not os.path.exists(path):
            return m.group(0)  # leave it for MkDocs to report
        kind = "tree" if os.path.isdir(path) else "blob"
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        return f"{m.group(1)}{repo}/{kind}/main/{rel}{m.group(3) or ''}{m.group(4)}"

    return LINK.sub(fix, markdown)
