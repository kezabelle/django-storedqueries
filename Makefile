help:
	@echo "clean-build - get rid of build artifacts & metadata"
	@echo "clean-pyc - get rid of dross files"
	@echo "dist - build a distribution; calls test, clean-build and clean-pyc"
	@echo "check - check the quality of the built distribution; calls dist for you"
	@echo "patch - Update the version by patch"
	@echo "release - register and upload to PyPI"

clean-build:
	rm -fr build/
	rm -fr htmlcov/
	rm -fr dist/
	rm -fr .eggs/
	find . -name '*.egg-info' -exec rm -fr {} +
	find . -name '*.egg' -exec rm -f {} +


clean-pyc:
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +

dist: clean-build clean-pyc
	python setup.py sdist bdist_wheel

check: dist
	pip install check-manifest pyroma restview
	check-manifest
	pyroma .
	restview --long-description

patch: clean-build clean-pyc
	pip install bumpversion
	bumpversion patch

release: patch dist
	pip install wheel twine
	twine upload dist/*


