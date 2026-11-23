/* Dedicated DDS -> PNG pipeline for TheHerta4. No Python or Blender API.
 * Each call owns all mutable state; callers parallelize independent files.
 * BC7: Herta decoder with standard endpoints. BC6H: pinned bcdec implementation.
 * PNG filtering/container: Herta. DEFLATE/CRC: pinned libdeflate (MIT).
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <math.h>
#include <emmintrin.h>
#include "vendor/libdeflate/libdeflate.h"
#define BCDEC_STATIC
#define BCDEC_IMPLEMENTATION
#include "vendor/bcdec/bcdec.h"
#define API __declspec(dllexport)
__declspec(dllimport) int __stdcall QueryPerformanceCounter(int64_t *);
__declspec(dllimport) int __stdcall QueryPerformanceFrequency(int64_t *);
int herta_dds_decode_bc7(const uint8_t *, size_t, unsigned, unsigned, uint8_t *, size_t);

typedef struct {
    uint32_t width, height, depth, dxgi;
    uint64_t input_bytes, output_bytes;
    double read_ms, decode_ms, filter_ms, compress_ms, write_ms;
} HertaPngStats;

static double now(void) {
    int64_t t,f; QueryPerformanceCounter(&t); QueryPerformanceFrequency(&f);
    return 1000.0*(double)t/(double)f;
}
static uint32_t le32(const uint8_t *p) {
    return (uint32_t)p[0] | (uint32_t)p[1]<<8 | (uint32_t)p[2]<<16 | (uint32_t)p[3]<<24;
}
static void be32(uint8_t *p,uint32_t x) {
    p[0]=(uint8_t)(x>>24);p[1]=(uint8_t)(x>>16);p[2]=(uint8_t)(x>>8);p[3]=(uint8_t)x;
}
static uint16_t unorm16(float v) {
    /* Match DirectXTex's float32 multiply, add 0.5, then truncate. */
    if (!(v>0.0f)) return 0;
    if (v>=1.0f) return 65535;
    return (uint16_t)(v*65535.0f+0.5f);
}
static float half_float(uint16_t h) {
    uint32_t s=(uint32_t)(h&0x8000)<<16,e=(h>>10)&31,m=h&1023,b;
    float f;
    if (!e) return (h&0x8000 ? -1.0f : 1.0f)*(float)m*(1.0f/16777216.0f);
    b=s | (e==31 ? 0x7f800000u : (e+112)<<23) | m<<13;
    memcpy(&f,&b,4);return f;
}
static int chunk(FILE *f,const char kind[4],const uint8_t *data,size_t size) {
    uint8_t b[4];uint32_t crc;
    if (size>0x7fffffffu) return 0;
    be32(b,(uint32_t)size);
    if (fwrite(b,1,4,f)!=4 || fwrite(kind,1,4,f)!=4) return 0;
    if (size && fwrite(data,1,size,f)!=size) return 0;
    crc=libdeflate_crc32(0,kind,4);crc=libdeflate_crc32(crc,data,size);be32(b,crc);
    return fwrite(b,1,4,f)==4;
}

/* Return 1 only for formats with verified value/bit-depth semantics. Exotic
 * channel layouts, arrays/cubemaps and unsupported formats use texconv. */
typedef struct { unsigned w,h,format,srgb,depth,bpp;size_t offset,pitch; } Dds;
static int parse(const uint8_t *d,size_t n,Dds *a) {
    uint32_t flags,fc,rm,gm,bm,am;
    if(n<128 || memcmp(d,"DDS ",4) || le32(d+4)!=124 || le32(d+76)!=32) return 0;
    memset(a,0,sizeof(*a));a->h=le32(d+12);a->w=le32(d+16);a->offset=128;a->depth=8;
    if(!a->w || !a->h || (uint64_t)a->w*a->h>67108864 || (le32(d+112)&0x0020fe00)) return 0;
    flags=le32(d+80);fc=le32(d+84);
    if((flags&4) && fc==0x30315844) {
        if(n<148 || le32(d+132)!=3 || le32(d+136)&4 || le32(d+140)!=1) return 0;
        a->offset=148;a->format=le32(d+128);
    } else if(flags&4) {
        switch(fc) {
        case 111:a->format=54;break;case 112:a->format=34;break;case 113:a->format=10;break;
        case 114:a->format=41;break;case 115:a->format=16;break;case 116:a->format=2;break;
        default:return 0;
        }
    } else {
        if(le32(d+88)!=32 || !(flags&0x40)) return 0;
        rm=le32(d+92);gm=le32(d+96);bm=le32(d+100);am=le32(d+104);
        if(gm!=0xff00 || (am!=0xff000000 && am!=0)) return 0;
        if(rm==255 && bm==0xff0000 && am) a->format=28;
        else if(rm==0xff0000 && bm==255) a->format=am ? 87:88;
        else return 0;
    }
    switch(a->format) {
    case 29:case 91:case 93:case 99:a->srgb=1;break;
    }
    switch(a->format) {
    case 98:case 99:case 95:case 96:
        if(a->format<98)a->depth=16;
        return (uint64_t)((a->w+3)/4)*((a->h+3)/4)*16<=n-a->offset;
    case 28:case 29:case 87:case 88:case 91:case 93:a->bpp=4;break;
    case 10:a->bpp=8;a->depth=16;break;
    case 34:a->bpp=4;a->depth=16;break;
    case 54:a->bpp=2;a->depth=16;break;
    case 2:a->bpp=16;a->depth=16;break;
    case 6:a->bpp=12;a->depth=16;break;
    case 16:a->bpp=8;a->depth=16;break;
    case 41:a->bpp=4;a->depth=16;break;
    default:return 0;
    }
    a->pitch=(size_t)a->w*a->bpp;
    if(le32(d+8)&8) { if(le32(d+20)<a->pitch)return 0; a->pitch=le32(d+20); }
    return a->pitch*a->h<=n-a->offset;
}
static void decode(const uint8_t *data,size_t n,const Dds *a,uint8_t *out) {
    const uint8_t *s=data+a->offset;unsigned x,y,c;
    if(a->format==98 || a->format==99) {
        herta_dds_decode_bc7(s,n-a->offset,a->w,a->h,out,(size_t)a->w*a->h*4);return;
    }
    if(a->format==95 || a->format==96) {
        /* Decode directly to half bits. A per-call conversion table removes
         * millions of half->float->UNORM operations while preserving rounding.
         * No shared initialization/mutable tables between concurrent callers. */
        uint16_t block[48],lut[65536];unsigned bx,by,gw=(a->w+3)/4;
        for(x=0;x<65536;x++)lut[x]=unorm16(half_float((uint16_t)x));
        for(by=0;by<a->h;by+=4) for(bx=0;bx<a->w;bx+=4) {
            bcdec_bc6h_half(s+((size_t)(by/4)*gw+bx/4)*16,block,12,a->format==96);
            for(y=0;y<4 && by+y<a->h;y++) for(x=0;x<4 && bx+x<a->w;x++) {
                uint8_t *d=out+((size_t)(by+y)*a->w+bx+x)*8;
                for(c=0;c<3;c++){uint16_t v=lut[block[(y*4+x)*3+c]];d[c*2]=(uint8_t)(v>>8);d[c*2+1]=(uint8_t)v;}
                d[6]=d[7]=255;
            }
        }
        return;
    }
    for(y=0;y<a->h;y++) {
        const uint8_t *row=s+y*a->pitch;
        uint8_t *d=out+(size_t)y*a->w*(a->depth==16?8:4);
        if(a->format==28 || a->format==29){memcpy(d,row,(size_t)a->w*4);continue;}
        for(x=0;x<a->w;x++) {
            const uint8_t *p=row+(size_t)x*a->bpp;
            if(a->depth==8) {
                d[x*4]=p[2];d[x*4+1]=p[1];d[x*4+2]=p[0];d[x*4+3]=(a->format==88 || a->format==93)?255:p[3];
            } else {
                int half=a->format==10 || a->format==34 || a->format==54;
                unsigned channels=a->bpp/(half?2:4);
                for(c=0;c<4;c++) {
                    float v=c==3?1.0f:0.0f;uint16_t u;
                    if(c<channels || (channels==1 && c<3)) {
                        unsigned channel=channels==1?0:c;
                        if(half)v=half_float((uint16_t)p[channel*2]|(uint16_t)p[channel*2+1]<<8);
                        else memcpy(&v,p+channel*4,4);
                    }
                    u=unorm16(v);d[x*8+c*2]=(uint8_t)(u>>8);d[x*8+c*2+1]=(uint8_t)u;
                }
            }
        }
    }
}
static unsigned magnitude(uint8_t x){return x<128?x:256-x;}
static void filter_rows(const uint8_t *pixels,uint8_t *filtered,unsigned h,size_t row,unsigned bpp) {
    unsigned y;
    for(y=0;y<h;y++) {
        const uint8_t *cur=pixels+(size_t)y*row,*prev=y?cur-row:NULL;
        uint8_t *out=filtered+(size_t)y*(row+1)+1;
        size_t i,step=((row/256)/bpp)*bpp;uint64_t scores[3]={0,0,0};unsigned type=0;
        if(step<bpp)step=bpp;
        for(i=0;i<row;i+=step) {
            unsigned c;for(c=0;c<bpp && i+c<row;c++) {
                uint8_t v=cur[i+c];scores[0]+=magnitude(v);
                scores[1]+=magnitude((uint8_t)(v-(i+c>=bpp?cur[i+c-bpp]:0)));
                scores[2]+=magnitude((uint8_t)(v-(prev?prev[i+c]:0)));
            }
        }
        if(scores[1]<scores[type])type=1;
        if(scores[2]<scores[type])type=2;
        out[-1]=(uint8_t)type;i=0;
        if(!type){memcpy(out,cur,row);continue;}
        if(type==1){memcpy(out,cur,bpp);i=bpp;}
        for(;i+16<=row;i+=16) {
            __m128i v=_mm_loadu_si128((const __m128i *)(cur+i));
            __m128i p=type==1?_mm_loadu_si128((const __m128i *)(cur+i-bpp)):
                (prev?_mm_loadu_si128((const __m128i *)(prev+i)):_mm_setzero_si128());
            _mm_storeu_si128((__m128i *)(out+i),_mm_sub_epi8(v,p));
        }
        for(;i<row;i++)out[i]=(uint8_t)(cur[i]-(type==1?cur[i-bpp]:(prev?prev[i]:0)));
    }
}

API unsigned herta_dds_png_abi_version(void){return 1;}
/* Status: 0 success, 1 unsupported/invalid DDS, 2 IO, 3 memory/encode error.
 * The destination is a caller-owned temporary path, published atomically by
 * the Python wrapper only after success. Never modifies/deletes the source. */
API int herta_dds_to_png(const wchar_t *source,const wchar_t *dest,int level,HertaPngStats *stats) {
    FILE *f=NULL;uint8_t *data=NULL,*pixels=NULL,*filtered=NULL,*compressed=NULL;
    struct libdeflate_compressor *compressor=NULL;
    size_t n,row,raw_size,bound,zsize=0;int status=2;Dds a;HertaPngStats st={0};double t=now();
    if(!source || !dest || !stats || level<1 || level>12)return 1;
    f=_wfopen(source,L"rb");if(!f)goto done;
    if(_fseeki64(f,0,SEEK_END))goto done;
    {int64_t size=_ftelli64(f);if(size<128 || size>0x7fffffff)goto done;n=(size_t)size;}
    if(_fseeki64(f,0,SEEK_SET))goto done;
    data=(uint8_t *)malloc(n);if(!data){status=3;goto done;}
    if(fread(data,1,n,f)!=n)goto done;
    fclose(f);f=NULL;st.input_bytes=n;st.read_ms=now()-t;t=now();
    if(!parse(data,n,&a)){status=1;goto done;}
    st.width=a.w;st.height=a.h;st.depth=a.depth;st.dxgi=a.format;
    row=(size_t)a.w*(a.depth==16?8:4);raw_size=(row+1)*a.h;
    pixels=(uint8_t *)malloc(row*a.h);filtered=(uint8_t *)malloc(raw_size);
    if(!pixels || !filtered){status=3;goto done;}
    decode(data,n,&a,pixels);free(data);data=NULL;
    st.decode_ms=now()-t;t=now();
    filter_rows(pixels,filtered,a.h,row,a.depth==16?8:4);free(pixels);pixels=NULL;
    st.filter_ms=now()-t;t=now();
    compressor=libdeflate_alloc_compressor(level);if(!compressor){status=3;goto done;}
    bound=libdeflate_zlib_compress_bound(compressor,raw_size);
    compressed=(uint8_t *)malloc(bound);if(!compressed){status=3;goto done;}
    zsize=libdeflate_zlib_compress(compressor,filtered,raw_size,compressed,bound);
    if(!zsize){status=3;goto done;}
    free(filtered);filtered=NULL;libdeflate_free_compressor(compressor);compressor=NULL;
    st.compress_ms=now()-t;t=now();
    f=_wfopen(dest,L"wb");if(!f){status=2;goto done;}
    {uint8_t ihdr[13]={0},gamma[4],intent=0;int ok;
        be32(ihdr,a.w);be32(ihdr+4,a.h);ihdr[8]=(uint8_t)a.depth;ihdr[9]=6;
        ok=fwrite("\x89PNG\r\n\x1a\n",1,8,f)==8 && chunk(f,"IHDR",ihdr,13);
        if(a.srgb){be32(gamma,45455);ok=ok && chunk(f,"sRGB",&intent,1) && chunk(f,"gAMA",gamma,4);}
        ok=ok && chunk(f,"IDAT",compressed,zsize) && chunk(f,"IEND",NULL,0);
        st.output_bytes=(uint64_t)_ftelli64(f);
        if(fclose(f))ok=0;f=NULL;status=ok?0:2;
    }
    st.write_ms=now()-t;
done:
    if(f)fclose(f);
    if(compressor)libdeflate_free_compressor(compressor);
    free(data);free(pixels);free(filtered);free(compressed);*stats=st;
    return status;
}
