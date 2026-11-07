---
name: lyrion-release
description: Versioning and packaging rules for the Lyrion Kodi add-on. Use when making a change to this repo, bumping the add-on version, building a test ZIP, committing, pushing, or publishing a GitHub release.
---

# Lyrion add-on versioning and packaging

## Every small change

1. Bump the patch number in `addon.xml` by one (`x.x.n` to `x.x.n+1`), based on the version in `addon.xml`. Do not skip numbers or reuse a version.
2. Build the test ZIP into the repo root as `plugin.audio.lyrion-<version>-kodi21.zip`, from the working tree:
   - Stage the files from `git ls-files -c -o --exclude-standard`, copying each into a temp directory under `plugin.audio.lyrion/`.
   - Zip that directory so the archive's top folder is `plugin.audio.lyrion/`.
   - Do not include `.git`, existing ZIPs, `kodi.log`, `__pycache__`, or `.DS_Store`.
3. Verify the ZIP contains the new `addon.xml` version and the changed files.
4. Leave the change uncommitted. Do not commit, push, tag, or publish.

The ZIP files are gitignored by `plugin.audio.lyrion-*-kodi21.zip`. Keep old ZIPs in the repo root; do not delete them.

## Commit, push, and release (only when the user asks)

Only do this when the user explicitly says to commit, push, or release. Small changes accumulate until the user decides a release is warranted.

1. Commit only the changed files by name. Use a message that explains why. End it with the attribution line from the current session instructions (for example `Co-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>`).
2. Push `main`.
3. Build the release ZIP from the commit with `git archive --format=zip --prefix=plugin.audio.lyrion/ -o <out> HEAD -- . ':(exclude)plugin.audio.lyrion-1.4.12-kodi21.zip'`. The old 1.4.12 ZIP is tracked, and the release ZIP should not contain it.
4. Create tag `v<version>` and a GitHub release named `<version>` with `gh release create`, attaching the ZIP. Pass `--target <commit>`.
5. Use this release body:

```
### Changes
- <one bullet per user-visible change>

Install from the attached Kodi 21 ZIP package.
```

6. Verify with `gh release view v<version>` that the asset name and tag are correct.

## Notes

- Do not rewrite history or force-push. Revert with a new commit if a commit needs to come out.
- The `.claude/skills` directory and this file are part of the repo; commit them only when the user asks.
