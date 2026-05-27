import * as THREE from "three";
import { SparkRenderer, SplatMesh, SparkControls, SparkXr } from "@sparkjsdev/spark";

const DEFAULT_SCENE = "https://storage.googleapis.com/forge-dev-public/asundqui/cozy-xr/cozy_cottage-lod.spz";
const params = new URLSearchParams(location.search);
const sceneUrl = params.get("scene") || DEFAULT_SCENE;
// COLMAP's world frame is arbitrary — rx/ry/rz (degrees) let you align
// gravity with -Y without retraining. e.g. ?scene=...&rx=-90&rz=180
const degToRad = (s) => ((parseFloat(s) || 0) * Math.PI) / 180;
const rotX = degToRad(params.get("rx"));
const rotY = degToRad(params.get("ry"));
const rotZ = degToRad(params.get("rz"));

const RENDER_TIMEOUT_MS = 30_000;

const stats = new Stats();
document.body.appendChild(stats.dom);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.01, 1000);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(window.innerWidth, window.innerHeight);
document.body.appendChild(renderer.domElement);

window.addEventListener("resize", () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

const spark = new SparkRenderer({ renderer, extSplats: false, maxStdDev: Math.sqrt(4) });
scene.add(spark);

const localFrame = new THREE.Group();
scene.add(localFrame);
localFrame.add(camera);

const world = new SplatMesh({ url: sceneUrl });
world.position.set(0, -1, 0);
world.rotation.set(rotX, rotY, rotZ);
scene.add(world);

const controls = new SparkControls({ renderer, canvas: renderer.domElement });

const xr = new SparkXr({
  renderer,
  onMouseLeaveOpacity: 0.5,
  onReady: (supported) => console.log(`SparkXr ready: VR ${supported ? "supported" : "not supported"}`),
  onEnterXr: () => console.log("Enter XR"),
  onExitXr: () => console.log("Exit XR"),
});

let renderEnabled = true;
let lastMoved = performance.now();
const lastPos = new THREE.Vector3().setScalar(Number.NEGATIVE_INFINITY);
const lastDir = new THREE.Vector3();

renderer.setAnimationLoop((time, xrFrame) => {
  stats.begin();

  xr.updateControllers(camera);
  controls.update(localFrame);

  const now = performance.now();
  const dir = localFrame.getWorldDirection(new THREE.Vector3());
  if (localFrame.position.distanceTo(lastPos) > 0.0001 || dir.dot(lastDir) < 0.9999) {
    lastMoved = now;
    renderEnabled = true;
    lastPos.copy(localFrame.position);
    lastDir.copy(dir);
  }
  if (now - lastMoved > RENDER_TIMEOUT_MS) renderEnabled = false;

  if (renderEnabled) renderer.render(scene, camera);
  stats.end();
});
