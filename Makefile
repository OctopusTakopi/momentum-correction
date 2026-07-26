PYTHON ?= python3
MANIFEST := data/manifest.txt
START_MONTH ?= 2019-10
END_MONTH ?= 2026-06
WORKERS ?= 8

.PHONY: all discover download parse backtest report live-plan test \
        benchmark regime source-chart intraday-cache intraday clean-figures

all: download parse backtest

## Daily spot pipeline

discover:
	$(PYTHON) scripts/discover.py --start $(START_MONTH) --end $(END_MONTH) --output $(MANIFEST)

download: discover
	$(PYTHON) scripts/download.py $(MANIFEST)

parse:
	$(PYTHON) scripts/parse.py

backtest:
	$(PYTHON) scripts/backtest.py
	$(PYTHON) scripts/robustness.py --stage sensitivity --correction 0.5
	$(PYTHON) scripts/robustness.py --stage sensitivity --correction 1.0
	$(PYTHON) scripts/robustness.py --stage sensitivity --correction 1.5
	$(PYTHON) scripts/robustness.py --stage sensitivity --correction 2.0
	$(PYTHON) scripts/robustness.py --stage oos-controls
	$(PYTHON) scripts/robustness.py --stage oos-sensitivity --correction 0.5
	$(PYTHON) scripts/robustness.py --stage oos-sensitivity --correction 1.0
	$(PYTHON) scripts/robustness.py --stage oos-sensitivity --correction 1.5
	$(PYTHON) scripts/robustness.py --stage oos-sensitivity --correction 2.0
	$(PYTHON) scripts/robustness.py --stage friction --friction-index 0
	$(PYTHON) scripts/robustness.py --stage friction --friction-index 1
	$(PYTHON) scripts/robustness.py --stage friction --friction-index 2
	$(PYTHON) scripts/robustness.py --stage friction --friction-index 3
	$(PYTHON) scripts/benchmark.py
	$(PYTHON) scripts/regime.py
	$(PYTHON) scripts/source_chart.py
	$(MAKE) report

report:
	$(PYTHON) scripts/report.py

## Reconstruction hypotheses, reported as such and never promoted to baseline

benchmark:
	$(PYTHON) scripts/benchmark.py

regime:
	$(PYTHON) scripts/regime.py

source-chart:
	$(PYTHON) scripts/source_chart.py

## Hourly execution pipeline.
## Set MOMENTUM_CORRECTION_HOURLY_DIR and MOMENTUM_CORRECTION_FUNDING_DIR to
## local mirrors of the venue archives before running intraday-cache.

intraday-cache:
	$(PYTHON) scripts/intraday.py --build --workers $(WORKERS)
	$(PYTHON) scripts/funding.py  --build --workers $(WORKERS)

intraday:
	$(PYTHON) scripts/backtest_intraday.py --workers $(WORKERS)
	$(MAKE) report

## Utilities

live-plan:
	$(PYTHON) scripts/live_plan.py --equity 100000

test:
	$(PYTHON) -m unittest discover -s tests -v

clean-figures:
	rm -rf results/figures
