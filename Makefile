.PHONY: test skills-uat skills-uat-strict

HERMES_SKILLS_UAT_EVIDENCE_DIR ?= /tmp/hermes-skills-uat
HERMES_REAL_HOME ?= /Users/hermes/.hermes
HERMES_OBSIDIAN_VAULT ?= /Users/hermes/Library/Mobile Documents/iCloud~md~obsidian/Documents/My-Second-Brain
HERMES_FEEDBACK_TRANSCRIPT ?= $(HERMES_SKILLS_UAT_EVIDENCE_DIR)/current-production-feedback.txt
HERMES_HISTORICAL_FEEDBACK_IMAGE_REJECTION_SOURCE ?=
HERMES_HISTORICAL_FEEDBACK_IMAGE_REJECTION_LABEL ?= Image \#1
HERMES_HISTORICAL_FEEDBACK_IMAGE_REJECTION_REASON ?= lark_group_invite_qr_not_feedback_screenshot

test:
	scripts/run_tests.sh

skills-uat:
	@mkdir -p "$(HERMES_SKILLS_UAT_EVIDENCE_DIR)"
	@if [ ! -f "$(HERMES_FEEDBACK_TRANSCRIPT)" ]; then \
		printf '%s\n' '未提供第二问题正文' > "$(HERMES_FEEDBACK_TRANSCRIPT)"; \
	fi
	@set -e; \
	set -a; \
	if [ -f "$(HERMES_REAL_HOME)/.env" ]; then . "$(HERMES_REAL_HOME)/.env"; fi; \
	set +a; \
	uv run --extra test python scripts/skills_uat_matrix.py \
		--real-home "$(HERMES_REAL_HOME)" \
		--output "$(HERMES_SKILLS_UAT_EVIDENCE_DIR)/skills-uat-latest.json"; \
	uv run --extra test python scripts/skills_second_problem_trace.py \
		--root "$(CURDIR)" \
		--root "$(HERMES_OBSIDIAN_VAULT)" \
		--root "$(HERMES_REAL_HOME)" \
		--include-agent-history \
		--artifact-root "$(HERMES_SKILLS_UAT_EVIDENCE_DIR)" \
		--feedback-transcript-file "$(HERMES_FEEDBACK_TRANSCRIPT)" \
		--feedback-artifact-label "Image #1" \
		--feedback-artifact-label "Image #2" \
		--feedback-artifact-scenario offline_production_feedback_interruption_quote_resume \
		--output "$(HERMES_SKILLS_UAT_EVIDENCE_DIR)/second-problem-trace.json"; \
	if [ -n "$(HERMES_HISTORICAL_FEEDBACK_IMAGE_REJECTION_SOURCE)" ]; then \
		uv run --extra test python scripts/historical_feedback_image_review.py \
			--source "$(HERMES_HISTORICAL_FEEDBACK_IMAGE_REJECTION_SOURCE)" \
			--label "$(HERMES_HISTORICAL_FEEDBACK_IMAGE_REJECTION_LABEL)" \
			--reason "$(HERMES_HISTORICAL_FEEDBACK_IMAGE_REJECTION_REASON)" \
			--output "$(HERMES_SKILLS_UAT_EVIDENCE_DIR)/historical-image-reviews.json"; \
	fi; \
	uv run --extra test python scripts/gateway_process_evidence.py \
		--worktree "$(CURDIR)" \
		--output "$(HERMES_SKILLS_UAT_EVIDENCE_DIR)/gateway-process-evidence.json" || true; \
	uv run --extra test python scripts/skills_uat_completion_audit.py \
		--evidence-dir "$(HERMES_SKILLS_UAT_EVIDENCE_DIR)" \
		--worktree "$(CURDIR)" \
		--output "$(HERMES_SKILLS_UAT_EVIDENCE_DIR)/completion-audit-latest.json"

skills-uat-strict: skills-uat
	uv run --extra test python scripts/skills_uat_completion_audit.py \
		--evidence-dir "$(HERMES_SKILLS_UAT_EVIDENCE_DIR)" \
		--worktree "$(CURDIR)" \
		--output "$(HERMES_SKILLS_UAT_EVIDENCE_DIR)/completion-audit-strict.json" \
		--require-complete
