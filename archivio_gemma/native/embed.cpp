// AGPL-3.0-or-later. Persistent bounded pipe worker for the existing GGUF runtime.
#include "llama.h"
#include "mtmd.h"
#include "gpu_device.h"
#include <iostream>
#include <sstream>
#include <iomanip>
#include <vector>
#include <memory>
#include <stdexcept>
#include <cmath>
#include <algorithm>
#include <cstring>
#include <chrono>
template<class T,void(*F)(T*)> using owned=std::unique_ptr<T,decltype(F)>;
static void concise_log(enum ggml_log_level level,const char *text,void *) {
    if(level!=GGML_LOG_LEVEL_DEBUG)std::fputs(text,stderr);
}
static void print_vector(const float *v) {
    if(!v)throw std::runtime_error("Embedding assente");
    double norm=0;for(int i=0;i<768;i++){if(!std::isfinite(v[i]))throw std::runtime_error("Embedding non finito");norm+=double(v[i])*v[i];}
    norm=std::sqrt(norm);if(norm<1e-9)throw std::runtime_error("Embedding nullo");
    std::cout<<'['<<std::setprecision(9);for(int i=0;i<768;i++)std::cout<<(i?",":"")<<v[i]/norm;std::cout<<']';
}
int main(int argc,char **argv){
 try {
    if(argc!=5)throw std::runtime_error("Usage: embed model mmproj runtime cpu|vulkan");
    llama_log_set(concise_log,nullptr);mtmd_log_set(concise_log,nullptr);
    ggml_backend_load_all_from_path(argv[3]);llama_backend_init();
    bool gpu=std::string(argv[4])=="vulkan";
    ggml_backend_dev_t device=gpu?ggml_backend_dev_by_name("Vulkan0"):nullptr;
    if(gpu && (!device || !gemmadesk_gpu_type(ggml_backend_dev_type(device))))
      throw std::runtime_error("Vulkan0 non disponibile. Seleziona CPU e riprova.");
    ggml_backend_dev_t devices[]={device,nullptr};
    auto mp=llama_model_default_params();mp.n_gpu_layers=gpu?99:0;
    if(gpu)mp.devices=devices;
    owned<llama_model,llama_model_free> model(llama_model_load_from_file(argv[1],mp),llama_model_free);
    if(!model)throw std::runtime_error("Caricamento modello fallito");
    char arch[128]={};llama_model_meta_val_str(model.get(),"general.architecture",arch,sizeof(arch));
    if(std::string(arch)!="gemma-embedding2" || llama_model_n_embd_out(model.get())!=768)throw std::runtime_error("Modello incompatibile");
    auto mm=mtmd_context_params_default();mm.use_gpu=gpu;mm.device=device;mm.n_threads=8;mm.warmup=false;
    // Essays need no visual encoder. Load it only upon the first image request.
    owned<mtmd_context,mtmd_free> vision(nullptr,mtmd_free);
    auto cp=llama_context_default_params();cp.n_ctx=65536;cp.n_batch=cp.n_ubatch=8192;cp.n_seq_max=8;
    cp.n_threads=cp.n_threads_batch=8;cp.embeddings=true;cp.pooling_type=LLAMA_POOLING_TYPE_MEAN;
    cp.offload_kqv=gpu;cp.op_offload=gpu;
    owned<llama_context,llama_free> ctx(llama_init_from_model(model.get(),cp),llama_free);
    if(!ctx)throw std::runtime_error("Contesto non disponibile");
    const auto *vocab=llama_model_get_vocab(model.get());int dim=llama_model_n_embd_inp(model.get());
    std::cout<<"{\"ready\":true,\"backend\":\""<<(gpu?"Vulkan":"CPU")<<"\",\"device\":"
      <<std::quoted(gpu?ggml_backend_dev_description(device):"CPU",'"','\\')
      <<",\"integrated_gpu\":"<<(gpu && ggml_backend_dev_type(device)==GGML_BACKEND_DEVICE_TYPE_IGPU?"true":"false")<<",\"max_text_batch\":8}"<<std::endl;
    std::string line;
    while(std::getline(std::cin,line)){
      std::istringstream header(line);char type;size_t size;unsigned width=0,height=0;
      if(!(header>>type>>size) || size>6*1024*1024)throw std::runtime_error("Protocollo non valido");
      if(type=='B' || type=='T') {
        auto begin=std::chrono::steady_clock::now();
        size_t count=type=='B'?size:1;if(count<1 || count>8)throw std::runtime_error("Batch di testo fuori limite");
        std::vector<std::vector<llama_token>> tokens;size_t total=0;
        for(size_t seq=0;seq<count;seq++) {
          size_t length=size;
          if(type=='B') {std::string h;if(!std::getline(std::cin,h))throw std::runtime_error("Batch incompleto");std::istringstream hs(h);if(!(hs>>length) || length>128*1024)throw std::runtime_error("Testo fuori limite");}
          std::string data(length,'\0');std::cin.read(data.data(),length);if(size_t(std::cin.gcount())!=length)throw std::runtime_error("Testo incompleto");
          std::vector<llama_token> ids(length+32);int n=llama_tokenize(vocab,data.data(),data.size(),ids.data(),ids.size(),true,false);
          if(n<=0 || n>8192 || (total+=n)>8192)throw std::runtime_error("Batch oltre 8192 token");
          ids.resize(n);tokens.push_back(std::move(ids));
        }
        auto mem=llama_get_memory(ctx.get());if(mem)llama_memory_clear(mem,true);
        owned<llama_batch_ext,llama_batch_ext_free> batch(llama_batch_ext_init(ctx.get()),llama_batch_ext_free);
        for(size_t seq=0;seq<count;seq++)for(size_t i=0;i<tokens[seq].size();i++) {
          llama_pos pos=i;int index=llama_batch_ext_add_token(batch.get(),seq,tokens[seq][i]);
          if(index<0 || !llama_batch_ext_set_pos(batch.get(),index,&pos) || !llama_batch_ext_set_output_embd(batch.get(),index,true))throw std::runtime_error("Batch testo non valido");
        }
        if(llama_process(ctx.get(),LLAMA_PROCESS_TYPE_ENCODE,batch.get()))throw std::runtime_error("Inferenza testo fallita");
        double seconds=std::chrono::duration<double>(std::chrono::steady_clock::now()-begin).count();
        if(type=='T') {std::cout<<"{\"vector\":";print_vector(llama_get_embeddings_seq(ctx.get(),0));std::cout<<",\"tokens\":"<<total;}
        else {
          std::cout<<"{\"vectors\":[";for(size_t seq=0;seq<count;seq++){if(seq)std::cout<<',';print_vector(llama_get_embeddings_seq(ctx.get(),seq));}
          std::cout<<"],\"tokens\":[";for(size_t seq=0;seq<count;seq++)std::cout<<(seq?",":"")<<tokens[seq].size();std::cout<<']';
        }
        std::cout<<",\"seconds\":"<<seconds<<"}"<<std::endl;continue;
      }
      if(type=='I' && (!(header>>width>>height) || !width || !height || width>1344 || height>1344 || size!=size_t(width)*height*3))throw std::runtime_error("Immagine non valida");
      if(type!='I' && type!='T' && type!='C')throw std::runtime_error("Comando non valido");
      std::string data(size,'\0');std::cin.read(data.data(),size);if(size_t(std::cin.gcount())!=size)break;
      if(type=='C'){
        std::vector<llama_token> ids(size+32);int count=llama_tokenize(vocab,data.data(),data.size(),ids.data(),ids.size(),true,false);
        if(count<0)throw std::runtime_error("Tokenizzazione fallita");
        std::cout<<"{\"count\":"<<count<<"}"<<std::endl;continue;
      }
      if(!vision) {vision.reset(mtmd_init_from_file(argv[2],model.get(),mm));if(!vision)throw std::runtime_error("Caricamento encoder visivo fallito");}
      auto begin=std::chrono::steady_clock::now();
      owned<mtmd_input_chunks,mtmd_input_chunks_free> chunks(mtmd_input_chunks_init(),mtmd_input_chunks_free);
      owned<mtmd_bitmap,mtmd_bitmap_free> bitmap(nullptr,mtmd_bitmap_free);
      mtmd_input_text text{data.data(),data.size(),false,false};mtmd_input_part part{};
      if(type=='I'){bitmap.reset(mtmd_bitmap_init(width,height,(const unsigned char*)data.data()));part.bitmap=bitmap.get();}
      else part.text=&text;
      const mtmd_input_part *parts[]={&part};
      if(mtmd_tokenize_from_parts(vision.get(),chunks.get(),parts,1,true))throw std::runtime_error("Preparazione input fallita");
      size_t count=0;for(size_t i=0;i<mtmd_input_chunks_size(chunks.get());i++)count+=mtmd_input_chunk_get_n_tokens(mtmd_input_chunks_get(chunks.get(),i));
      if(!count || count>8192)throw std::runtime_error("Contesto oltre 8192 token");
      auto mem=llama_get_memory(ctx.get());if(mem)llama_memory_clear(mem,true);
      owned<llama_batch_ext,llama_batch_ext_free> batch(llama_batch_ext_init(ctx.get()),llama_batch_ext_free);
      std::vector<std::vector<float>> images;llama_pos pos=0;
      for(size_t c=0;c<mtmd_input_chunks_size(chunks.get());c++){
        const auto *chunk=mtmd_input_chunks_get(chunks.get(),c);size_t n=mtmd_input_chunk_get_n_tokens(chunk),nt=0;
        bool is_text=mtmd_input_chunk_get_type(chunk)==MTMD_INPUT_CHUNK_TYPE_TEXT;
        const auto *ids=is_text?mtmd_input_chunk_get_tokens_text(chunk,&nt):nullptr;
        if(!is_text){if(mtmd_encode_chunk(vision.get(),chunk))throw std::runtime_error("Encoder visivo fallito");const float *e=mtmd_get_output_embd(vision.get());images.emplace_back(e,e+n*dim);}
        for(size_t i=0;i<n;i++){
          int index=is_text?llama_batch_ext_add_token(batch.get(),0,ids[i]):llama_batch_ext_add_embd(batch.get(),0,{images.back().data()+i*dim,1,size_t(dim)});
          if(index<0 || !llama_batch_ext_set_pos(batch.get(),index,&pos) || !llama_batch_ext_set_output_embd(batch.get(),index,true))throw std::runtime_error("Batch non valido");pos++;
        }
      }
      if(llama_process(ctx.get(),LLAMA_PROCESS_TYPE_ENCODE,batch.get()))throw std::runtime_error("Inferenza fallita");
      const float *v=llama_get_embeddings_seq(ctx.get(),0);if(!v)throw std::runtime_error("Embedding assente");
      double norm=0;for(int i=0;i<768;i++){if(!std::isfinite(v[i]))throw std::runtime_error("Embedding non finito");norm+=double(v[i])*v[i];}
      norm=std::sqrt(norm);if(norm<1e-9)throw std::runtime_error("Embedding nullo");
      std::cout<<"{\"vector\":["<<std::setprecision(9);for(int i=0;i<768;i++)std::cout<<(i?",":"")<<v[i]/norm;std::cout<<"],\"tokens\":"<<count<<",\"seconds\":"<<std::chrono::duration<double>(std::chrono::steady_clock::now()-begin).count()<<"}"<<std::endl;
    }
    return 0;
 }catch(const std::exception &e){std::cerr<<e.what()<<std::endl;return 1;}
}
