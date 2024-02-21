import os
import cv2
import time
import tqdm
import numpy as np

import torch
import torch.nn.functional as F

import rembg

from cam_utils import orbit_camera, OrbitCamera
from gs_renderer_4d import Renderer, MiniCam

from grid_put import mipmap_linear_grid_put_2d
from mesh import Mesh, safe_normalize

import copy

from utils.general_utils import build_rotation


def get_knn_features(x, idx):
    # x: num_points x C; idx: num_points x k
    num_points = x.shape[0]
    k = idx.shape[1]

    idx = idx.view(-1) # [num_points x k, ]

    feature = (x[idx, :]).view(num_points, k, -1).contiguous() # num_points, k, C
    return feature


def quaternion_inverse(x):
    # x: num_points, _, 4; w x y z
    # x_comj = copy.deepcopy(x)
    x_comj = torch.cat([x[:, :, 0:1], -x[:, :, 1:]], dim=-1)

    x_norm = torch.norm(x, p=2, dim=-1, keepdim=True) # num_points, _, 1
    x_inv = x_comj / (x_norm ** 2)

    return x_inv

class GUI:
    def __init__(self, opt):
        self.opt = opt  # shared with the trainer's opt to support in-place modification of rendering parameters.
        self.gui = opt.gui # enable gui
        self.W = opt.W
        self.H = opt.H
        self.cam = OrbitCamera(opt.W, opt.H, r=opt.radius, fovy=opt.fovy)

        self.mode = "image"
        # self.seed = "random"
        self.seed = 888

        self.buffer_image = np.ones((self.W, self.H, 3), dtype=np.float32)
        self.need_update = True  # update buffer_image

        # models
        self.device = torch.device("cuda")
        self.bg_remover = None

        self.guidance_sd = None
        self.guidance_zero123 = None
        self.guidance_svd = None


        self.enable_sd = False
        self.enable_zero123 = False
        self.enable_svd = False


        # renderer
        self.renderer = Renderer(sh_degree=self.opt.sh_degree, T=self.opt.batch_size)
        self.gaussain_scale_factor = 1

        # input image
        self.input_img = None
        self.input_mask = None
        self.input_img_torch = None
        self.input_mask_torch = None
        self.overlay_input_img = False
        self.overlay_input_img_ratio = 0.5

        self.input_img_list = None
        self.input_mask_list = None
        self.input_img_torch_list = None
        self.input_mask_torch_list = None

        # input text
        self.prompt = ""
        self.negative_prompt = ""

        # training stuff
        self.training = False
        self.optimizer = None
        self.step = 0
        self.train_steps = 1  # steps per rendering loop
        
        # load input data from cmdline
        if self.opt.input is not None: # True
            self.load_input(self.opt.input) # load imgs, if has bg, then rm bg; or just load imgs
        
        # override prompt from cmdline
        if self.opt.prompt is not None: # None
            self.prompt = self.opt.prompt

        # override if provide a checkpoint
        if self.opt.load is not None: # not None
            self.renderer.initialize(self.opt.load)  
            # self.renderer.gaussians.load_model(opt.outdir, opt.save_path)             
        else:
            # initialize gaussians to a blob
            self.renderer.initialize(num_pts=self.opt.num_pts)

        self.seed_everything()

    def seed_everything(self):
        try:
            seed = int(self.seed)
        except:
            seed = np.random.randint(0, 1000000)

        print(f'Seed: {seed:d}')
        os.environ["PYTHONHASHSEED"] = str(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = True

        self.last_seed = seed

    def prepare_train(self):

        self.step = 0

        # setup training
        self.renderer.gaussians.training_setup(self.opt)

        # # do not do progressive sh-level
        self.renderer.gaussians.active_sh_degree = self.renderer.gaussians.max_sh_degree
        self.optimizer = self.renderer.gaussians.optimizer

        # default camera
        pose = orbit_camera(self.opt.elevation, 0, self.opt.radius)
        self.fixed_cam = MiniCam(
            pose,
            self.opt.ref_size,
            self.opt.ref_size,
            self.cam.fovy,
            self.cam.fovx,
            self.cam.near,
            self.cam.far,
        )

        self.enable_sd = self.opt.lambda_sd > 0 and self.prompt != "" # False
        self.enable_zero123 = self.opt.lambda_zero123 > 0 # True
        self.enable_svd = self.opt.lambda_svd > 0 and self.input_img is not None # False

        # lazy load guidance model
        if self.guidance_sd is None and self.enable_sd: # False
            if self.opt.mvdream: # False
                print(f"[INFO] loading MVDream...")
                from guidance.mvdream_utils import MVDream
                self.guidance_sd = MVDream(self.device)
                print(f"[INFO] loaded MVDream!")
            else:
                print(f"[INFO] loading SD...")
                from guidance.sd_utils import StableDiffusion
                self.guidance_sd = StableDiffusion(self.device)
                print(f"[INFO] loaded SD!")

        if self.guidance_zero123 is None and self.enable_zero123: # True
            print(f"[INFO] loading zero123...")
            from guidance.zero123_utils import Zero123
            self.guidance_zero123 = Zero123(self.device, t_range=[0.02, self.opt.t_max])
            print(f"[INFO] loaded zero123!")


        if self.guidance_svd is None and self.enable_svd: # False
            print(f"[INFO] loading SVD...")
            from guidance.svd_utils import StableVideoDiffusion
            self.guidance_svd = StableVideoDiffusion(self.device)
            print(f"[INFO] loaded SVD!")

        # input image
        if self.input_img is not None:
            self.input_img_torch = torch.from_numpy(self.input_img).permute(2, 0, 1).unsqueeze(0).to(self.device)
            self.input_img_torch = F.interpolate(self.input_img_torch, (self.opt.ref_size, self.opt.ref_size), mode="bilinear", align_corners=False)

            self.input_mask_torch = torch.from_numpy(self.input_mask).permute(2, 0, 1).unsqueeze(0).to(self.device)
            self.input_mask_torch = F.interpolate(self.input_mask_torch, (self.opt.ref_size, self.opt.ref_size), mode="bilinear", align_corners=False)

        if self.input_img_list is not None:
            self.input_img_torch_list = [torch.from_numpy(input_img).permute(2, 0, 1).unsqueeze(0).to(self.device) for input_img in self.input_img_list]
            self.input_img_torch_list = [F.interpolate(input_img_torch, (self.opt.ref_size, self.opt.ref_size), mode="bilinear", align_corners=False) for input_img_torch in self.input_img_torch_list]
            
            self.input_mask_torch_list = [torch.from_numpy(input_mask).permute(2, 0, 1).unsqueeze(0).to(self.device) for input_mask in self.input_mask_list]
            self.input_mask_torch_list = [F.interpolate(input_mask_torch, (self.opt.ref_size, self.opt.ref_size), mode="bilinear", align_corners=False) for input_mask_torch in self.input_mask_torch_list]
        # prepare embeddings
        with torch.no_grad():

            if self.enable_sd:
                self.guidance_sd.get_text_embeds([self.prompt], [self.negative_prompt])

            if self.enable_zero123:
                c_list, v_list = [], []
                for _ in range(self.opt.n_views):
                    for input_img_torch in self.input_img_torch_list:
                        c, v = self.guidance_zero123.get_img_embeds(input_img_torch)
                        c_list.append(c)
                        v_list.append(v)
                self.guidance_zero123.embeddings = [torch.cat(c_list, 0), torch.cat(v_list, 0)]
            
            if self.enable_svd:
                self.guidance_svd.get_img_embeds(self.input_img)

    def train_step(self):
        starter = torch.cuda.Event(enable_timing=True)
        ender = torch.cuda.Event(enable_timing=True)
        starter.record()

        for _ in range(self.train_steps): # 1

            self.step += 1 # self.step starts from 0
            step_ratio = min(1, self.step / self.opt.iters) # 1, step / 500

            # update lr
            self.renderer.gaussians.update_learning_rate(self.step)

            loss = 0

            self.renderer.prepare_render()
        
            ### known view
            for b_idx in range(self.opt.batch_size): # 14
                cur_cam = copy.deepcopy(self.fixed_cam)
                cur_cam.time = b_idx
                out = self.renderer.render(cur_cam)

                # rgb loss
                image = out["image"].unsqueeze(0) # [1, 3, H, W] in [0, 1]
                loss = loss + 10000 * step_ratio * F.mse_loss(image, self.input_img_torch_list[b_idx]) / self.opt.batch_size

                # mask loss
                mask = out["alpha"].unsqueeze(0) # [1, 1, H, W] in [0, 1]
                loss = loss + 1000 * step_ratio * F.mse_loss(mask, self.input_mask_torch_list[b_idx]) / self.opt.batch_size

            ### novel view (manual batch)
            render_resolution = 128 if step_ratio < 0.3 else (256 if step_ratio < 0.6 else 512)
            # render_resolution = 512
            images = []
            poses = []
            vers, hors, radii = [], [], []
            # avoid too large elevation (> 80 or < -80), and make sure it always cover [-30, 30]
            min_ver = max(min(-30, -30 - self.opt.elevation), -80 - self.opt.elevation)
            max_ver = min(max(30, 30 - self.opt.elevation), 80 - self.opt.elevation)

            for _ in range(self.opt.n_views):
                for b_idx in range(self.opt.batch_size):

                    # render random view
                    ver = np.random.randint(min_ver, max_ver)
                    hor = np.random.randint(-180, 180)
                    radius = 0

                    vers.append(ver)
                    hors.append(hor)
                    radii.append(radius)

                    pose = orbit_camera(self.opt.elevation + ver, hor, self.opt.radius + radius)
                    poses.append(pose)

                    cur_cam = MiniCam(pose, render_resolution, render_resolution, self.cam.fovy, self.cam.fovx, self.cam.near, self.cam.far, time=b_idx)

                    bg_color = torch.tensor([1, 1, 1] if np.random.rand() > self.opt.invert_bg_prob else [0, 0, 0], dtype=torch.float32, device="cuda")
                    out = self.renderer.render(cur_cam, bg_color=bg_color)

                    image = out["image"].unsqueeze(0) # [1, 3, H, W] in [0, 1]
                    images.append(image)

                    # enable mvdream training
                    if self.opt.mvdream: # False
                        for view_i in range(1, 4):
                            pose_i = orbit_camera(self.opt.elevation + ver, hor + 90 * view_i, self.opt.radius + radius)
                            poses.append(pose_i)

                            cur_cam_i = MiniCam(pose_i, render_resolution, render_resolution, self.cam.fovy, self.cam.fovx, self.cam.near, self.cam.far)

                            # bg_color = torch.tensor([0.5, 0.5, 0.5], dtype=torch.float32, device="cuda")
                            out_i = self.renderer.render(cur_cam_i, bg_color=bg_color)

                            image = out_i["image"].unsqueeze(0) # [1, 3, H, W] in [0, 1]
                            images.append(image)


            images = torch.cat(images, dim=0)

            # guidance loss
            if self.enable_sd:
                if self.opt.mvdream:
                    loss = loss + self.opt.lambda_sd * self.guidance_sd.train_step(images, poses, step_ratio)
                else:
                    loss = loss + self.opt.lambda_sd * self.guidance_sd.train_step(images, step_ratio)

            if self.enable_zero123:
                loss = loss + self.opt.lambda_zero123 * self.guidance_zero123.train_step(images, vers, hors, radii, step_ratio) / (self.opt.batch_size * self.opt.n_views)

            if self.enable_svd:
                loss = loss + self.opt.lambda_svd * self.guidance_svd.train_step(images, step_ratio)

            # optimize step
            loss.backward()
            self.optimizer.step()
            self.optimizer.zero_grad()

            # densify and prune
            if self.step >= self.opt.density_start_iter and self.step <= self.opt.density_end_iter:
                viewspace_point_tensor, visibility_filter, radii = out["viewspace_points"], out["visibility_filter"], out["radii"]
                self.renderer.gaussians.max_radii2D[visibility_filter] = torch.max(self.renderer.gaussians.max_radii2D[visibility_filter], radii[visibility_filter])
                self.renderer.gaussians.add_densification_stats(viewspace_point_tensor, visibility_filter)

                if self.step % self.opt.densification_interval == 0:
                    # size_threshold = 20 if self.step > self.opt.opacity_reset_interval else None
                    self.renderer.gaussians.densify_and_prune(self.opt.densify_grad_threshold, min_opacity=0.01, extent=0.5, max_screen_size=1)
                
                if self.step % self.opt.opacity_reset_interval == 0:
                    self.renderer.gaussians.reset_opacity()

        ender.record()
        torch.cuda.synchronize()
        t = starter.elapsed_time(ender)

        self.need_update = True

    
    def load_input(self, file):
        file_list = [os.path.join(file, f'{x}.png') for x in range(self.opt.batch_size)]
        self.input_img_list, self.input_mask_list = [], []
        for file in file_list:
            # load image
            print(f'[INFO] load image from {file}...')
            img = cv2.imread(file, cv2.IMREAD_UNCHANGED)
            if img.shape[-1] == 3:
                raise
                if self.bg_remover is None:
                    self.bg_remover = rembg.new_session()
                img = rembg.remove(img, session=self.bg_remover)
                cv2.imwrite(file.replace('.png', '_rgba.png'), img) 
            img = cv2.resize(img, (self.W, self.H), interpolation=cv2.INTER_AREA)
            img = img.astype(np.float32) / 255.0
            input_mask = img[..., 3:]
            # white bg
            input_img = img[..., :3] * input_mask + (1 - input_mask)
            # bgr to rgb
            input_img = input_img[..., ::-1].copy()
            self.input_img_list.append(input_img)
            self.input_mask_list.append(input_mask)

    @torch.no_grad()
    def save_model(self, mode='geo', texture_size=1024, t=0):
        os.makedirs(self.opt.outdir, exist_ok=True)
        if mode == 'geo':
            path = f'logs/{opt.save_path}_mesh_{t:03d}.ply'
            mesh = self.renderer.gaussians.extract_mesh_t(path, self.opt.density_thresh, t=t)
            mesh.write_ply(path)

        elif mode == 'geo+tex':
            path = f'logs/{opt.save_path}_mesh_{t:03d}.obj'
            mesh = self.renderer.gaussians.extract_mesh_t(path, self.opt.density_thresh, t=t)

            # perform texture extraction
            print(f"[INFO] unwrap uv...")
            h = w = texture_size
            mesh.auto_uv()
            mesh.auto_normal()

            albedo = torch.zeros((h, w, 3), device=self.device, dtype=torch.float32)
            cnt = torch.zeros((h, w, 1), device=self.device, dtype=torch.float32)

            # self.prepare_train() # tmp fix for not loading 0123
            # vers = [0]
            # hors = [0]
            vers = [0] * 8 + [-45] * 8 + [45] * 8 + [-89.9, 89.9]
            hors = [0, 45, -45, 90, -90, 135, -135, 180] * 3 + [0, 0]

            render_resolution = 512

            import nvdiffrast.torch as dr

            if not self.opt.force_cuda_rast and (not self.opt.gui or os.name == 'nt'):
                glctx = dr.RasterizeGLContext()
            else:
                glctx = dr.RasterizeCudaContext()

            for ver, hor in zip(vers, hors):
                # render image
                pose = orbit_camera(ver, hor, self.cam.radius)

                cur_cam = MiniCam(
                    pose,
                    render_resolution,
                    render_resolution,
                    self.cam.fovy,
                    self.cam.fovx,
                    self.cam.near,
                    self.cam.far,
                    time=t
                )
                
                cur_out = self.renderer.render(cur_cam)

                rgbs = cur_out["image"].unsqueeze(0) # [1, 3, H, W] in [0, 1]
                    
                # get coordinate in texture image
                pose = torch.from_numpy(pose.astype(np.float32)).to(self.device)
                proj = torch.from_numpy(self.cam.perspective.astype(np.float32)).to(self.device)

                v_cam = torch.matmul(F.pad(mesh.v, pad=(0, 1), mode='constant', value=1.0), torch.inverse(pose).T).float().unsqueeze(0)
                v_clip = v_cam @ proj.T
                rast, rast_db = dr.rasterize(glctx, v_clip, mesh.f, (render_resolution, render_resolution))

                depth, _ = dr.interpolate(-v_cam[..., [2]], rast, mesh.f) # [1, H, W, 1]
                depth = depth.squeeze(0) # [H, W, 1]

                alpha = (rast[0, ..., 3:] > 0).float()

                uvs, _ = dr.interpolate(mesh.vt.unsqueeze(0), rast, mesh.ft)  # [1, 512, 512, 2] in [0, 1]

                # use normal to produce a back-project mask
                normal, _ = dr.interpolate(mesh.vn.unsqueeze(0).contiguous(), rast, mesh.fn)
                normal = safe_normalize(normal[0])

                # rotated normal (where [0, 0, 1] always faces camera)
                rot_normal = normal @ pose[:3, :3]
                viewcos = rot_normal[..., [2]]

                mask = (alpha > 0) & (viewcos > 0.5)  # [H, W, 1]
                mask = mask.view(-1)

                uvs = uvs.view(-1, 2).clamp(0, 1)[mask]
                rgbs = rgbs.view(3, -1).permute(1, 0)[mask].contiguous()
                
                # update texture image
                cur_albedo, cur_cnt = mipmap_linear_grid_put_2d(
                    h, w,
                    uvs[..., [1, 0]] * 2 - 1,
                    rgbs,
                    min_resolution=256,
                    return_count=True,
                )
                
                # albedo += cur_albedo
                # cnt += cur_cnt
                mask = cnt.squeeze(-1) < 0.1
                albedo[mask] += cur_albedo[mask]
                cnt[mask] += cur_cnt[mask]

            mask = cnt.squeeze(-1) > 0
            albedo[mask] = albedo[mask] / cnt[mask].repeat(1, 3)

            mask = mask.view(h, w)

            albedo = albedo.detach().cpu().numpy()
            mask = mask.detach().cpu().numpy()

            # dilate texture
            from sklearn.neighbors import NearestNeighbors
            from scipy.ndimage import binary_dilation, binary_erosion

            inpaint_region = binary_dilation(mask, iterations=32)
            inpaint_region[mask] = 0

            search_region = mask.copy()
            not_search_region = binary_erosion(search_region, iterations=3)
            search_region[not_search_region] = 0

            search_coords = np.stack(np.nonzero(search_region), axis=-1)
            inpaint_coords = np.stack(np.nonzero(inpaint_region), axis=-1)

            knn = NearestNeighbors(n_neighbors=1, algorithm="kd_tree").fit(
                search_coords
            )
            _, indices = knn.kneighbors(inpaint_coords)

            albedo[tuple(inpaint_coords.T)] = albedo[tuple(search_coords[indices[:, 0]].T)]

            mesh.albedo = torch.from_numpy(albedo).to(self.device)
            mesh.write(path)
        else:
            path = os.path.join(self.opt.outdir, self.opt.save_path + '_4d_model.ply')
            self.renderer.gaussians.save_ply(path)

        print(f"[INFO] save model to {path}.")

    # no gui mode
    def train(self, iters=500, ui=False):
        image_list =[]
        from PIL import Image
        from diffusers.utils import export_to_video, export_to_gif
        if self.gui:
            from visergui import ViserViewer
            self.viser_gui = ViserViewer(device="cuda", viewer_port=8080)
        if iters > 0:
            self.prepare_train()
            if self.gui:
                self.viser_gui.set_renderer(self.renderer, self.fixed_cam)
            
            for i in tqdm.trange(iters): # 500
                self.train_step()
        os.makedirs(f'eval_data/{opt.save_path}', exist_ok=True)
        # render eval
        image_list =[]
        nframes = self.opt.batch_size * 8 + 15 * 8 # 14 * 5
        hor = 180
        delta_hor = 45 / 15 # 360 / nframes
        # time = 0
        delta_time = 1
        for i in range(8):
            time = 0
            for j in range(self.opt.batch_size + 15):
                pose = orbit_camera(self.opt.elevation, hor-180, self.opt.radius)
                cur_cam = MiniCam(
                    pose,
                    512,
                    512,
                    self.cam.fovy,
                    self.cam.fovx,
                    self.cam.near,
                    self.cam.far,
                    time=time
                )
                with torch.no_grad():
                    outputs = self.renderer.render(cur_cam)

                out = outputs["image"].cpu().detach().numpy().astype(np.float32)
                out = np.transpose(out, (1, 2, 0))
                out = Image.fromarray(np.uint8(out*255))
                image_list.append(out)

                time = (time + delta_time) % self.opt.batch_size
                if j >= self.opt.batch_size:
                    hor = (hor+delta_hor) % 360


        export_to_gif(image_list, f'vis_data/{opt.save_path}.gif')

if __name__ == "__main__":
    import argparse
    from omegaconf import OmegaConf

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="path to the yaml config file")
    args, extras = parser.parse_known_args()

    # override default config from cli
    opt = OmegaConf.merge(OmegaConf.load(args.config), OmegaConf.from_cli(extras))


    # auto find mesh from stage 1
    opt.load = os.path.join(opt.outdir, opt.save_path + '_model.ply')

    gui = GUI(opt)

    gui.train(opt.iters)