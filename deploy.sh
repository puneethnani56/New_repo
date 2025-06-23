#!/bin/bash

# Exit on error
set -e

# Read the first argument passed (environment name)
ENVIRONMENT=$1

echo "Starting deployment to $ENVIRONMENT..."

# Simulated deployment steps
echo "Pulling latest code for $ENVIRONMENT..."
sleep 1

echo "Installing dependencies..."
sleep 1

echo "Starting services for $ENVIRONMENT..."
sleep 1

echo "✅ Deployment to $ENVIRONMENT completed!"
