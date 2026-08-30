.PHONY: run test demo verify-deps serve demo-v2 build-single verify-single \
        demo-differentiation security-demo prove-isolated prove-reproducible judge

# Zero-dependency Python project -- "build" is just running it.
# Requires nothing beyond a Python 3 interpreter.

run:
	python3 chronovault.py $(ARGS)

test:
	python3 -m unittest discover tests -v

demo:
	python3 chronovault.py demo demo-project --snapshot
	@echo
	@echo "Demo repo ready at ./demo-project -- cd in and try 'vault status', 'vault serve', etc."

verify-deps:
	python3 scripts/check_dependencies.py | tee deps-proof.txt

serve:
	python3 chronovault.py serve

demo-v2:
	bash scripts/demo_v2.sh

build-single:
	python3 scripts/build_single_file.py

verify-single: build-single
	bash scripts/verify_single_file.sh

demo-differentiation:
	bash scripts/demo_differentiation.sh

security-demo:
	python3 scripts/security_demo.py

prove-isolated:
	bash scripts/prove_isolated_mode.sh

prove-reproducible:
	python3 scripts/prove_reproducible.py

judge:
	python3 scripts/judge_mode.py
