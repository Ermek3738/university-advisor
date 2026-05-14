#!/bin/bash
cd backend

# Import universities if the CSV exists
if [ -f "data/universities_real.csv" ]; then
  echo "Importing universities..."
  python import_universities.py --file data/universities_real.csv --update
else
  echo "No CSV found, skipping import (tables will be created empty)"
fi

uvicorn main:app --host 0.0.0.0 --port $PORT