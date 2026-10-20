#!/bin/sh
# Build the JoinMarket-NG XBT + Jam image from committed sources only.
#   xbt-docker/build.sh [JAM_REPO_DIR] [TAG]
# The Jam fork is taken from git (branch/commit given by JAM_REF, default "xbt").
set -eu
REPO=$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)
JAM_DIR=${1:-../jam-xbt}
TAG=${2:-jm-xbt:0.41.0-xbt1}
JAM_REF=${JAM_REF:-xbt}
PLATFORM=${PLATFORM:-linux/amd64}
BUILDER=${BUILDER:-}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/jm" "$work/jam"
git -C "$REPO" archive HEAD | tar -x -C "$work/jm"
git -C "$JAM_DIR" archive "$JAM_REF" | tar -x -C "$work/jam"

JM_COMMIT=$(git -C "$REPO" rev-parse HEAD)
JAM_COMMIT=$(git -C "$JAM_DIR" rev-parse "$JAM_REF")
EPOCH=$(git -C "$REPO" log -1 --format=%ct)
echo "joinmarket-ng $JM_COMMIT, jam $JAM_COMMIT, platform $PLATFORM"

docker buildx build ${BUILDER:+--builder "$BUILDER"} \
    --platform "$PLATFORM" \
    --build-context jam="$work/jam" \
    --build-arg SOURCE_DATE_EPOCH="$EPOCH" \
    --build-arg JOINMARKET_BUILD_COMMIT="$JM_COMMIT" \
    --build-arg JOINMARKET_BUILD_REF=xbt \
    --label org.opencontainers.image.revision="$JM_COMMIT" \
    --label org.kilombino.jam.revision="$JAM_COMMIT" \
    --label org.opencontainers.image.source=https://github.com/Kilombino/joinmarket-ng \
    -f "$work/jm/xbt-docker/Dockerfile" -t "$TAG" --load "$work/jm"
