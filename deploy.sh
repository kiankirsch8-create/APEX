#!/bin/bash
echo "Finding latest Cursor branch..."
BRANCH=$(git branch --sort=-committerdate | grep "cursor/" | head -1 | xargs)
echo "Merging: $BRANCH"
git checkout main
git merge "$BRANCH"
git push origin main
echo "Deployed to Railway"
