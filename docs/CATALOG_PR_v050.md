# Catalog PR prep — ceo-pack v0.5.0 (do NOT file yet)

Adrian files this PR himself (or says go) after the v0.5.0-dev branch is pushed
and reviewed. One file changes in NousResearch/hermes-agent:
`plugin-catalog/ceo-pack.yaml`. Same route as PR #155549 (v0.4.0) and #157203
(v0.4.1): pencil-edit the file in the GitHub web UI (auto-forks), branch
`ceo-pack-0.5.0`, validate first with `scripts/validate_plugin_catalog.py`.

## The image line (the install-card thumbnail)

Layout copied from the advanced-delegation catalog entry
(`plugin-catalog/advanced-delegation.yaml` in the hermes-agent checkout):
the catalog `image:` is a `raw.githubusercontent.com` URL pointing at an
`assets/` file in the plugin repo, pinned to the entry's SHA — so the
thumbnail is as immutable as the sha pin. The validator
(`scripts/validate_plugin_catalog.py`, `_is_allowed_image_url`) only accepts
https URLs on `raw.githubusercontent.com` / `github.com` /
`*.githubusercontent.com`.

```
image: https://raw.githubusercontent.com/tradersurfer/ceo-pack/c1dd341617aa63823b0c1c356a1aa08120ae5926/assets/ceo-pack-cover-v040.webp
```

`c1dd3416...` is the HEAD of `v0.5.0-dev` after the v2.2 push (2026-11-19 ~23:2x).
Re-pin if the branch moves again before filing. Do NOT substitute the v0.4.1
sha (6331b08a) — the image must resolve at the pinned v0.5.0 commit, and
`assets/ceo-pack-cover-v040.webp` is in the v0.5.0 tree (447,488 bytes,
1920x1280, verified present in this repo copy).

VERIFIED 2026-11-19 ~23:21: the URL above returns HTTP 200 at that SHA and
the bytes are JPEG (JFIF, 1920x1280). Chromium `<img>` sniffs image magic
bytes and renders JPEG regardless of the `.webp` extension, so this URL works
as the card thumbnail. The `.jpg` rename below is still the cleaner option,
not a blocker.

URL format verified against the validator's own rule (2026-11-18, Phase 4):
`_is_allowed_image_url` returns True for this shape with a 40-hex SHA.

## Entry changes (vs the v0.4.1 entry, PR #157203)

- `sha`: 6331b08a03f18c202a292eb8c4741b69aad69994 → `<V050_SHA>`
- `version`: `"0.4.1"` → `"0.5.0"`
- `description`: ELI5, matching the rewritten `plugin.yaml` description —
  "Your AI C-suite inside Hermes. Say what your business needs in plain
  words — the pack plans it, assigns each job to the right seat, checks the
  work, and stops for your approval before anything big, priced, or hard to
  undo." (Replaces the v0.4.1 wording; the plugin.yaml description carries
  the longer version with the short dev note.)
- `image`: add the line above (new — v0.4.0/v0.4.1 entries had no image,
  which is why the install card has no thumbnail).
- `requires_hermes`: `">=0.22.5"` (already the plugin.yaml value for v0.5.0;
  confirm against the entry — v0.4.1 said `>=0.21.6`).

## Known quirk — flag for Adrian, do not silently rename

`assets/ceo-pack-cover-v040.webp` contains **JPEG bytes** (JFIF,
1920x1280), not WebP, despite the `.webp` extension. It has lived in the
repo under this name since v0.4.0 without being referenced by any catalog
entry, so nothing live breaks today. The `.webp` URL is verified loading
(200, JPEG bytes, 2026-11-19) and Chromium renders it by sniffing, so the
catalog PR can file with it as-is. The cleaner option is still renaming it
to `assets/ceo-pack-cover-v040.jpg` — Adrian's one-liner from his laptop
clone, then the `image:` line changes extension and the SHA re-pins:

```
git mv assets/ceo-pack-cover-v040.webp assets/ceo-pack-cover-v040.jpg
git commit -m "Rename cover to .jpg (JPEG bytes)" && git push origin v0.5.0-dev
```

Note: the local README already links the `.jpg` name — do NOT push that
README line until the `.jpg` exists on the branch, or the README image
breaks. (The binary rename cannot be pushed from the VM — push-files is
text-only.)

## Pre-flight (before filing)

1. Push `v0.5.0-dev` to GitHub; record the HEAD SHA.
2. Confirm the image URL loads in a browser at that SHA.
3. Run `scripts/validate_plugin_catalog.py` on the edited entry (rc=0).
4. File the PR; note the fork-PR checks wait for a Nous maintainer to
   approve the run (normal hold, not a rejection).
