"""Run with Blender --background --factory-startup --python this_file.py."""
import bpy,importlib.util,numpy as np,json,time,os
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'common/shapekey_native.py'
spec=importlib.util.spec_from_file_location('shapekey_native_test',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
rng=np.random.default_rng(221);n=4000
mesh=bpy.data.meshes.new('differential');mesh.from_pydata(rng.normal(size=(n,3)).tolist(),[],[])
obj=bpy.data.objects.new('differential',mesh);bpy.context.scene.collection.objects.link(obj)
basis=obj.shape_key_add(name='Basis');keys=[]
for i in range(40):
 key=obj.shape_key_add(name=f'K{i:03}');a=rng.normal(size=(n,3)).astype(np.float32);key.data.foreach_set('co',a.ravel());key.value=0
 if i%3==2:key.relative_key=keys[-1]
 keys.append(key)
assert not m.fast_path_reason(obj),m.fast_path_reason(obj)
def evaluated():
 bpy.context.view_layer.update();eo=obj.evaluated_get(bpy.context.evaluated_depsgraph_get());em=eo.to_mesh();a=np.empty((len(em.vertices),3),np.float32);em.vertices.foreach_get('co',a.ravel());eo.to_mesh_clear();return a
t=time.perf_counter();old_basis=evaluated();old={}
for key in keys:
 key.value=1;old[key.name]=evaluated();key.value=0
slow=time.perf_counter()-t
results=[]
for native in ('0','1'):
 os.environ['HERTA_SHAPEKEY_NATIVE']=native
 for mirror in (False,True):
  t=time.perf_counter();base,rows=m.capture_relative_shapes(obj,keys,mirror=mirror);new=dict(rows);elapsed=time.perf_counter()-t
  expected_basis=old_basis.copy()
  if mirror:expected_basis[:,0]*=-1
  assert base.tobytes()==expected_basis.tobytes(),'basis mismatch'
  for name,a in old.items():
   expected=a.copy()
   if mirror:expected[:,0]*=-1
   if new[name].tobytes()!=expected.tobytes():
    diff=np.abs(new[name]-expected);raise AssertionError((native,mirror,name,float(diff.max()),int(np.count_nonzero(diff))))
  results.append({'native':native,'mirror':mirror,'seconds':elapsed,'byte_equal':True})
print('SHAPEKEY_DIFFERENTIAL='+json.dumps({'blender':bpy.app.version_string,'native_loaded':m.native_library() is not None,'keys':len(keys),'vertices':n,'legacy_seconds':slow,'results':results}))
