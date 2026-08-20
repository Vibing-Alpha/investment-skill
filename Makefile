# Stock Analysis System v7 — end-user targets.
# (The full developer Makefile — test / audit / ci / release / publish — is not
#  shipped; this is the use-only product.)

PY ?= python3

.PHONY: setup
setup:                 ## Guided first-run setup (API key, strategy, holdings)
	$(PY) -m scripts.distribute bootstrap

.PHONY: update-check
update-check:          ## Is a newer skill release available?
	$(PY) -m scripts.update check --force

.PHONY: update
update:                ## Fast-forward to the latest release (opt-in, never clobbers)
	$(PY) -m scripts.update apply

.PHONY: help
help:
	@echo "make setup         — first-run setup (API key, strategy, holdings)"
	@echo "make update-check  — check for a newer skill release"
	@echo "make update        — apply the latest release"

# Personal track record: assemble one quarter's summary.md from the archive +
# journal + pinned benchmark. See .claude/skills/track-record/SKILL.md.
#   make track-record QUARTER=2026Q3
.PHONY: track-record
track-record:
	$(PY) -m scripts.track_record report --quarter $(QUARTER)
