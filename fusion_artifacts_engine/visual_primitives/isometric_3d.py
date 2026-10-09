import logging

from fusion_artifacts_engine.visual_primitives.base import BaseCompiler, _esc

logger = logging.getLogger(__name__)

_THREEJS_CDN = "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js"


class Isometric3DCompiler(BaseCompiler):
    # 5-6年级 立体几何/切削展开。DSL → HTML+Three.js 3D 场景。
    # 唯一使用 Three.js 的基元（其余纯 SVG）。
    # 动画：cut / unfold / volume peel。

    visual_type = "isometric_3d"

    def compile(self, dsl_dict: dict | None, progress: float = 1.0) -> str:
        entities = self._entities(dsl_dict)
        params = self._parameters(dsl_dict)
        active = set(self._active_entities(dsl_dict, progress))
        scene_js = self._build_scene_js(entities, params, active, progress)
        body = "<div id='three-canvas' style='width:600px;height:400px;'></div>"
        script = f"<script src='{_THREEJS_CDN}'></script><script>{scene_js}</script>"
        return self._html_wrap(self._title(dsl_dict), body, "", script)

    def _build_scene_js(self, entities: list, params: dict, active: set, progress: float) -> str:
        if not entities:
            ents_js = "[{id:'box',type:'box',w:2,h:2,d:2,color:0x4dabf7}]"
        else:
            ents_list = []
            for ent in entities:
                if not isinstance(ent, dict):
                    continue
                eid = ent.get("id") or "ent"
                etype = (ent.get("type") or "box").lower()
                props = ent.get("properties") or {}
                if not isinstance(props, dict):
                    props = {}
                w = float(props.get("width") or props.get("w") or 2)
                h = float(props.get("height") or props.get("h") or 2)
                d = float(props.get("depth") or props.get("d") or 2)
                color = 0x4DABF7
                if "red" in str(props.get("color") or ""):
                    color = 0xFF6B6B
                elif "green" in str(props.get("color") or ""):
                    color = 0x69DB7C
                opacity = 0.85 if eid in active else 0.5
                ents_list.append(
                    f"{{id:'{_esc(eid)}',type:'{etype}',w:{w},h:{h},d:{d},"
                    f"color:{color},opacity:{opacity}}}"
                )
            ents_js = "[" + ",".join(ents_list) + "]"
        return (
            "(function(){"
            "if(typeof THREE==='undefined'){"
            "document.getElementById('three-canvas').innerHTML="
            "'<p>Three.js failed to load</p>';return;}"
            "var container=document.getElementById('three-canvas');"
            "var scene=new THREE.Scene();"
            "var camera=new THREE.PerspectiveCamera(50,1.5,0.1,100);"
            "camera.position.set(5,5,5);camera.lookAt(0,0,0);"
            "var renderer=new THREE.WebGLRenderer({antialias:true});"
            "renderer.setSize(600,400);container.appendChild(renderer.domElement);"
            "scene.add(new THREE.AmbientLight(0xffffff,0.6));"
            "var dl=new THREE.DirectionalLight(0xffffff,0.8);"
            "dl.position.set(5,10,5);scene.add(dl);"
            f"var ents={ents_js};"
            "var meshes=[];"
            "ents.forEach(function(e){"
            "var geo=new THREE.BoxGeometry(e.w,e.h,e.d);"
            "var mat=new THREE.MeshLambertMaterial({"
            "color:e.color,transparent:true,opacity:e.opacity});"
            "var m=new THREE.Mesh(geo,mat);m.userData.id=e.id;"
            "m.position.x=(meshes.length%3-1)*3;"
            "scene.add(m);meshes.push(m);"
            "});"
            "function animate(){requestAnimationFrame(animate);"
            "meshes.forEach(function(m){m.rotation.y+=0.01;});"
            "renderer.render(scene,camera);}"
            "animate();"
            "})();"
        )
