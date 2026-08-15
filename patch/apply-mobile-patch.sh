#!/bin/bash
# Apply mobile responsive patches to QuantDesk container
CONTAINER="quantdesk"
DIR="$(dirname "$0")"

docker cp "$DIR/mobile.css" "$CONTAINER:/app/web/dist/assets/mobile.css"
docker cp "$DIR/mobile.js" "$CONTAINER:/app/web/dist/assets/mobile.js"
docker cp "$DIR/index.html" "$CONTAINER:/app/web/dist/index.html"
docker exec "$CONTAINER" chmod 644 /app/web/dist/assets/mobile.css /app/web/dist/assets/mobile.js
echo "Mobile patches applied to $CONTAINER"
