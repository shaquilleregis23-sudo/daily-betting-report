#!/bin/bash
# Run this once before any model scripts.
# Installs all Python libraries needed for the prediction system.

echo "Installing prediction model dependencies..."
pip install \
  pybaseball \
  nba_api \
  xgboost \
  scikit-learn \
  pandas \
  numpy \
  joblib \
  requests

echo ""
echo "Done. Run the collector with:"
echo "  python3 /Users/shaquilleregis/Documents/model/1_collect_historical.py"
