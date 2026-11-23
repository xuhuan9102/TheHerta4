// Batched relative shape evaluation. C ABI, strict float32, no Blender access.
#include <algorithm>
#include <cstdint>
#include <thread>
#include <vector>
extern "C" __declspec(dllexport) unsigned herta_shapekey_abi() noexcept {return 1;}
extern "C" __declspec(dllexport) int herta_relative_shapes(
 const float* basis,const float* const* keys,const float* const* relatives,
 uint64_t coordinates,uint64_t count,unsigned threads,int mirror,float* output) noexcept {
 if(!basis||!keys||!relatives||!output||!coordinates||!count||coordinates%3)return 0;
 if(count>UINT64_MAX/coordinates)return 0;
 for(uint64_t k=0;k<count;++k)if(!keys[k]||!relatives[k])return 0;
 const uint64_t total=coordinates*count;
 threads=std::max(1u,std::min(threads,32u));
 if(total<262144)threads=1;
 auto work=[&](unsigned t){
   uint64_t begin=total/threads*t,end=t+1==threads?total:total/threads*(t+1);
   while(begin<end){
     uint64_t key=begin/coordinates,offset=begin%coordinates;
     uint64_t stop=std::min(coordinates,offset+end-begin);
     const float* src=keys[key];const float* ref=relatives[key];
     float* dst=output+key*coordinates;
     for(uint64_t j=offset;j<stop;++j){
       float delta=src[j]-ref[j];
       float value=basis[j]+delta;
       dst[j]=(mirror&&j%3==0)?-value:value;
     }
     begin+=stop-offset;
   }
 };
 // Joining jthreads also makes partial thread creation safe on resource exhaustion.
 try {std::vector<std::jthread> pool;pool.reserve(threads-1);
      for(unsigned t=1;t<threads;++t)pool.emplace_back(work,t);work(0);}
 catch(...){return 0;}
 return 1;
}

#include <cstring>
static uint16_t half_bits(float value) noexcept {
 uint32_t bits;std::memcpy(&bits,&value,4);
 uint16_t sign=uint16_t((bits>>16)&0x8000u);
 uint32_t mantissa=bits&0x7fffffu,exponent=(bits>>23)&255u;
 if(exponent==255u){
   if(!mantissa)return uint16_t(sign|0x7c00u);
   uint16_t payload=uint16_t(mantissa>>13);
   return uint16_t(sign|0x7c00u|(payload?payload:1u));
 }
 int e=int(exponent)-112;
 if(e>=31)return uint16_t(sign|0x7c00u);
 if(e<=0){
   if(e < -10)return sign;
   mantissa|=0x800000u;
   unsigned shift=unsigned(14-e);
   uint32_t rounded=mantissa>>shift,remaining=mantissa&((1u<<shift)-1u);
   uint32_t halfway=1u<<(shift-1);
   if(remaining>halfway||(remaining==halfway&&(rounded&1u)))++rounded;
   return uint16_t(sign|rounded);
 }
 uint32_t rounded=mantissa+0xfffu+((mantissa>>13)&1u);
 return uint16_t(sign|((uint32_t(e)<<10)+(rounded>>13)));
}
extern "C" __declspec(dllexport) int herta_pack_half_float3(
 const float* values,uint64_t count,unsigned threads,uint32_t* output) noexcept {
 if(!count)return 1;if(!values||!output||count>UINT64_MAX/3)return 0;
 uint64_t pairs=(count+1)/2;
 threads=std::max(1u,std::min(threads,32u));if(count<65536)threads=1;
 auto work=[&](unsigned t){
   uint64_t start=pairs/threads*t,stop=t+1==threads?pairs:pairs/threads*(t+1);
   for(uint64_t pair=start;pair<stop;++pair){
     uint64_t i=pair*6;
     uint32_t a=half_bits(values[i]),b=half_bits(values[i+1]),c=half_bits(values[i+2]);
     uint32_t d=0,e=0,f=0;
     if(pair*2+1<count){d=half_bits(values[i+3]);e=half_bits(values[i+4]);f=half_bits(values[i+5]);}
     output[pair*3]=a|(b<<16);output[pair*3+1]=c|(d<<16);output[pair*3+2]=e|(f<<16);
   }
 };
 try {std::vector<std::jthread> pool;pool.reserve(threads-1);
      for(unsigned t=1;t<threads;++t)pool.emplace_back(work,t);work(0);}
 catch(...){return 0;}
 return 1;
}

#include <cmath>
extern "C" __declspec(dllexport) int herta_pack_deltas(
 const float* base,const float* target,uint64_t vertices,unsigned stride,
 const uint32_t* columns,unsigned channels,int64_t global_start,unsigned threads,
 float* packed,int32_t* index_map,uint64_t* active_count) noexcept {
 if(!base||!target||!columns||!packed||!index_map||!active_count||!stride||!channels||global_start<0)return 0;
 if(vertices>uint64_t(INT32_MAX)||uint64_t(global_start)+vertices>uint64_t(INT32_MAX)||vertices>SIZE_MAX/channels)return 0;
 for(unsigned c=0;c<channels;++c)if(columns[c]>=stride)return 0;
 threads=std::max(1u,std::min(threads,32u));if(vertices<32768)threads=1;
 try {
   std::vector<float> deltas(size_t(vertices)*channels);
   std::vector<uint8_t> active(vertices);
   std::vector<uint64_t> counts(threads,0),offsets(threads,0);
   auto bounds=[&](unsigned t){return std::pair<uint64_t,uint64_t>{vertices/threads*t,t+1==threads?vertices:vertices/threads*(t+1)};};
   auto parallel=[&](auto fn){std::vector<std::jthread> pool;pool.reserve(threads-1);for(unsigned t=1;t<threads;++t)pool.emplace_back(fn,t);fn(0);};
   parallel([&](unsigned t){auto [begin,end]=bounds(t);uint64_t count=0;
     for(uint64_t v=begin;v<end;++v){bool changed=false;
       for(unsigned c=0;c<channels;++c){float a=base[v*stride+columns[c]],b=target[v*stride+columns[c]];
         float delta=b-a;deltas[v*channels+c]=delta==0.0f?0.0f:delta;
         float distance=std::fabs(a-b),relative=1.0e-5f*std::fabs(b),tolerance=1.0e-6f+relative;
         bool close=(a==b)||(std::isfinite(a)&&std::isfinite(b)&&distance<=tolerance);
         changed=changed||!close;
       }
       active[v]=changed;count+=changed;
     }counts[t]=count;
   });
   uint64_t total=0;for(unsigned t=0;t<threads;++t){offsets[t]=total;total+=counts[t];}
   parallel([&](unsigned t){auto [begin,end]=bounds(t);uint64_t out=offsets[t];
     for(uint64_t v=begin;v<end;++v){
       index_map[v]=-1;
       if(active[v]){std::memcpy(packed+out*channels,deltas.data()+v*channels,channels*sizeof(float));index_map[v]=int32_t(uint64_t(global_start)+out);++out;}
     }
   });
   *active_count=total;return 1;
 }catch(...){return 0;}
}
