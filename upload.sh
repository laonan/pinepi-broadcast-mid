#!/bin/bash

# Upload script for ws-mid deployment
# Usage: bash upload.sh [user@server]

set -e

DEFAULT_SERVER="user@your-server"
SERVER="${1:-$DEFAULT_SERVER}"
REMOTE_DIR="/tmp/pinepi-broadcast-mid-upload"

echo "Uploading pinepi-broadcast-mid to $SERVER..."

# Create remote directory
ssh "$SERVER" "mkdir -p $REMOTE_DIR"

# Check if config.json exists on remote
if ssh "$SERVER" "[ ! -f /etc/pinepi-broadcast-mid/config.json ]"; then
    echo "Remote config.json not found, will upload config.json.example"
    # Upload all files including config.json.example
    rsync -av --exclude='config.json' --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
        ./ "$SERVER:$REMOTE_DIR/"
else
    echo "Remote config.json exists, preserving it"
    # Upload all files except config.json (to preserve existing config)
    rsync -av --exclude='config.json' --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
        ./ "$SERVER:$REMOTE_DIR/"
fi

echo "Upload complete!"
echo ""
echo "Next steps on server:"
echo "  ssh $SERVER"
echo "  cd $REMOTE_DIR"
echo "  sudo bash install.sh"
echo ""
echo "Note: Existing config.json will be preserved"
