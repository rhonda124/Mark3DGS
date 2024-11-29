## Setup

For installation:
```shell
conda env create --file environment.yml
conda activate c3dgs
```

## Running

```shell
python train.py -s <path to COLMAP> --eval
e.g. python train.py -s ./data/tandt/train --eval
```

## Evaluation
```shell
python render.py -m <path to trained model> --max_hashmap <max hash size of the model>
e.g. python render.py -m ./output/00514bfa-2 --max_hashmap 19
```

## Watermark Extraction
```shell
python extract_watermark.py -m <path to trained model>
e.g. python extract_watermark.py -m ./output/00514bfa-2
```
* To modify the embedded watermark value, please edit the watermark parameters in `./scene/gaussian_model.py`.

## Convert
```shell
python convert.py -s <path to source>
e.g. python convert.py -s ./data/OmniObject3D/asparagus
```