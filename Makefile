.PHONY: check validate selftest

## check: run every quality gate (what CI runs)
check: validate selftest

## validate: validate all SKILL.md files, evals, and plugin manifests
validate:
	python3 scripts/validate_skills.py

## selftest: offline unit checks for scripts that define one (no device, no adb)
selftest:
	@for s in skills/*/scripts/*.py; do \
	  grep -q 'def self_test' "$$s" || continue; \
	  printf '%s: ' "$$s"; \
	  python3 "$$s" --self-test > /tmp/st.$$$$ 2>&1 || { cat /tmp/st.$$$$; rm -f /tmp/st.$$$$; exit 1; }; \
	  tail -1 /tmp/st.$$$$; rm -f /tmp/st.$$$$; \
	done
