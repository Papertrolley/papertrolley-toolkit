blender --background LiAZ-52926503_1-2-0_Songz.blend --python ../../scripts/transfer_blender.py -- \
    --layout LiAZ-52926503-1-2-0_600dpi.quads.json \
    --photo front_left.jpg --photo-meta front_left.vertices.json \
    --out result.png \
    --bg transparent --verbose \
    --expand 1 --expand-alpha 255
