#!/bin/bash
set -e

RPI_USER="$1"
RPI_HOST="$2"
RPI_PASS="$3"

if [ -z "$RPI_HOST" ]; then
    echo "Usage: ./deploy_remote.sh <user> <host> [password]"
    exit 1
fi

echo "Deploying to $RPI_USER@$RPI_HOST..."

# Function to run SSH command
run_ssh() {
    if [ -n "$RPI_PASS" ] && command -v sshpass &> /dev/null; then
        sshpass -p "$RPI_PASS" ssh -o StrictHostKeyChecking=no "$RPI_USER@$RPI_HOST" "$1"
    else
        ssh -o StrictHostKeyChecking=no "$RPI_USER@$RPI_HOST" "$1"
    fi
}

# Function to run SCP
run_scp() {
    if [ -n "$RPI_PASS" ] && command -v sshpass &> /dev/null; then
        sshpass -p "$RPI_PASS" scp -o StrictHostKeyChecking=no -r "$1" "$RPI_USER@$RPI_HOST:$2"
    else
        scp -o StrictHostKeyChecking=no -r "$1" "$RPI_USER@$RPI_HOST:$2"
    fi
}

echo "1. Stopping existing service (if running)..."
run_ssh "sudo systemctl stop greenmind-gateway || true"

echo "2. Cleaning up old files..."
run_ssh "rm -rf ~/greenmind-gateway"

echo "3. Copying new files..."
run_scp "greenmind-gateway" "~/"

echo "4. Running install script..."
# Using 'echo' to pipe password to sudo if needed? 
# The install script uses sudo. If the user needs password for sudo, it might fail non-interactively.
# We'll assume the user has password-less sudo OR we pipe the password.
if [ -n "$RPI_PASS" ]; then
    run_ssh "echo '$RPI_PASS' | sudo -S ~/greenmind-gateway/scripts/install.sh"
else
    run_ssh "sudo ~/greenmind-gateway/scripts/install.sh"
fi

echo "Deployment complete!"
