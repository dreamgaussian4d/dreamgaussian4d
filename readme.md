<div align="center">

<h1>DreamGaussian4D:<br>Generative 4D Gaussian Splatting</h1>


</div>

## Install
```bash
# install customized diffusers
pip install ./diffusers

pip install -r requirements.txt

# a modified gaussian splatting (+ depth, alpha rendering)
git clone --recursive https://github.com/ashawkey/diff-gaussian-rasterization
pip install ./diff-gaussian-rasterization

# simple-knn
pip install ./simple-knn

# nvdiffrast
pip install git+https://github.com/NVlabs/nvdiffrast/

# kiuikit
pip install git+https://github.com/ashawkey/kiuikit

```

Tested on:
*  python 3.8 & torch 2.1 & CUDA 11.8 on an 80GB A100.
    * Stage I: 2min. Stage II: 4.5min. Stage III: 3.5min.
*  python 3.8 & torch 2.0 & CUDA 11.6 on an 24GB A5000.
    *  Stage I: 4.5min. Stage II: 9min. Stage III: 5.5min with `oom_hack`.

## Image-to-4D
```bash
# generate driving video
python gen_vid.py --name anya_rgba --seed 42 --bg white

# Stage I: train 500 iters (~2min) and export ckpt & coarse_mesh to logs
python main.py --config configs/image.yaml input=data/anya_rgba.png save_path=anya

# Stage II: temporal optimization stage (export meshes by default)
python main_4d.py --config configs/4d.yaml input=data/anya_rgba.png save_path=anya

python main_4d.py  --config configs/4d.yaml input=data/CONSISTENT4D_DATA/in-the-wild/blooming_rose save_path=blooming_rose
python main_4d.py  --config configs/4d.yaml input=data/CONSISTENT4D_DATA/synthetic/dancing_patrick_star save_path=dancing_patrick_star
python main_4d.py  --config configs/4d.yaml input=data/CONSISTENT4D_DATA/synthetic/walking_astronaut save_path=walking_astronaut
python main_4d.py  --config configs/4d.yaml input=data/CONSISTENT4D_DATA/synthetic/firing_pistol save_path=firing_pistol

python main_4d.py  --config configs/4d.yaml input=data/test_dataset/input/aurorus save_path=aurorus

# Stage III: texture optimization (optional, it requires large GPU memory and we are optimzing it)
python main2_4d.py --config configs/4d_svd.yaml input=data/anya_rgba.png save_path=anya

# python main2_4d.py --config configs/4d_svd.yaml input=data/CONSISTENT4D_DATA/in-the-wild/blooming_rose save_path=blooming_rose

# to turn on viser GUI, add `gui=True`, e.g.:
python main.py --config configs/image.yaml input=data/anya_rgba.png save_path=anya gui=True
```
Meshes will be automatically exported to `logs` in Stage II. Visulizations will be saved to `vis_data`.

## Video-to-4D
```bash
# Stage I: train 500 iters (~2min) and export ckpt & coarse_mesh to logs
python main.py --config configs/image.yaml input=data/CONSISTENT4D_DATA/in-the-wild/blooming_rose/0.png save_path=blooming_rose

# Stage II: temporal optimization stage (export meshes by default)
python main_4d_video.py --config configs/4d.yaml input=data/anya_rgba.png save_path=anya
```

## Load exported meshes in Blender
- Install the [Stop-motion-OBJ
](https://github.com/neverhood311/Stop-motion-OBJ) add-on
- File -> Import -> Mesh Sequence
- Go to `logs` directory, type in the file name (e.g., 'anya'), and tick `Material per Frame`.

