# Assistente LuzViva
# 1. busca nos documentos os 3 trechos mais parecidos com a pergunta (RAG)
# 2. se a pergunta tiver uma placa (ex.: IP0699800), pega os dados da luminária (ferramenta consultar_fila)
# 3. o modelo Qwen lê esse contexto e responde em JSON
# Rodar:  uvicorn api:app --port 8001   e abrir http://localhost:8001

import json
import os
import re
import time

import torch
import xgrammar as xgr
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer
from transformers import AutoModelForCausalLM, AutoTokenizer, LogitsProcessor

PASTA = os.path.dirname(os.path.abspath(__file__))
LLM = "Qwen/Qwen2.5-1.5B-Instruct"
LIMIAR = 0.86  # nota mínima da busca para responder (escolhida no notebook)
RECUSA = "Não consta na base do LuzViva."

documentos = [json.loads(linha) for linha in open(f"{PASTA}/base/documentos.jsonl", encoding="utf-8")]
luminarias = json.load(open(f"{PASTA}/base/luminarias.json", encoding="utf-8"))


# ---------- 1. busca ----------
# cada trecho vira um vetor de números; trechos com sentido parecido têm vetores parecidos
embedder = SentenceTransformer("intfloat/multilingual-e5-small")
vetores = embedder.encode(["passage: " + d["texto"] for d in documentos], normalize_embeddings=True)


# Busca os k trechos mais parecidos com a pergunta e devolve cada um com a sua nota
def buscar(pergunta, k=3):
    q = embedder.encode(["query: " + pergunta], normalize_embeddings=True)[0]
    notas = vetores @ q  # parecença de 0 a 1 entre a pergunta e cada trecho
    melhores = notas.argsort()[::-1][:k]
    return [(documentos[i], float(notas[i])) for i in melhores]


# ---------- 2. ferramenta: dados da luminária ----------

# Acha a placa da luminária escrita na pergunta (ex.: IP0699800)
def achar_placa(pergunta):
    m = re.search(r"IP\d{7}", pergunta.upper())
    return m.group(0) if m else None


# Ferramenta do agente: devolve os dados da luminária com essa placa, ou None se não existir
def consultar_fila(placa):
    for linha in luminarias:
        if linha["placa"] == placa:
            return linha
    return None


# Transforma os dados da luminária em texto para o modelo (um dado por frase: o modelo pequeno erra menos)
def texto_fila(l):
    data = l["ultima_medicao"][8:10] + "/" + l["ultima_medicao"][5:7] + " às " + l["ultima_medicao"][11:16]
    ok = {"apagada": "não, a luminária está apagada", "insuficiente": "não, a luz está fraca (insuficiente)"}.get(l["situacao"], "sim")
    visao = f"sim, com confiança {l['confianca_visao']:.2f}" if l["confianca_visao"] else "não"
    return (f"Luminária {l['placa']}, {l['endereco']} ({l['subprefeitura']}). Situação: {l['situacao']}. "
            f"Luz funcionando normalmente (está ok?): {ok}. "
            f"Último lux medido: {l['ultimo_lux']} em {data}. Relatos de cidadãos: {l['ocorrencias_app']}. "
            f"Visão computacional apontou falha: {visao}. "
            f"Chamado aberto: {'sim, em atendimento' if l['chamado_aberto'] else 'não'}. "
            f"Prioridade: {l['prioridade']} de 100.")


# ---------- 3. modelo ----------
FORMATO = ('Responda apenas com um JSON neste formato: {"resposta": "...", "fontes": '
           '[{"titulo": "título da fonte", "trecho": "frase curta copiada da fonte"}], '
           '"recusou": false, "confianca": 0.0}')

# config A: o modelo responde sozinho, sem documentos
PROMPT_SEM_CONTEXTO = ("Você é o assistente do LuzViva, que ajuda gestores e cidadãos de São Paulo com a "
                       "manutenção da iluminação pública. Responda em português, em uma ou duas frases. "
                       f'Se não souber, responda "{RECUSA}" e marque recusou como true. ' + FORMATO)

# configs B e C: o modelo só pode usar o CONTEXTO (trechos + dados da luminária)
PROMPT_RAG = ("Você é o assistente do LuzViva, que ajuda gestores e cidadãos de São Paulo com a manutenção "
              "da iluminação pública. Responda em português, em uma ou duas frases, usando SOMENTE as "
              "informações do CONTEXTO. Em fontes, use o título exato da fonte usada. Se o contexto não "
              f'tiver a resposta, responda "{RECUSA}" e marque recusou como true. Nunca informe nome, '
              "telefone ou e-mail de cidadãos. " + FORMATO)

ESQUEMA = {"type": "object", "required": ["resposta", "fontes", "recusou", "confianca"], "properties": {
    "resposta": {"type": "string"},
    "fontes": {"type": "array", "items": {"type": "object", "required": ["titulo", "trecho"],
                                          "properties": {"titulo": {"type": "string"}, "trecho": {"type": "string"}}}},
    "recusou": {"type": "boolean"},
    "confianca": {"type": "number"}}}

tok = modelo = gramatica = None

# Carrega o modelo só na primeira pergunta (demora alguns segundos)
def carregar():
    
    global tok, modelo, gramatica
    if modelo is None:
        tok = AutoTokenizer.from_pretrained(LLM)
        modelo = AutoModelForCausalLM.from_pretrained(LLM, dtype=torch.float16, device_map="auto")
        info = xgr.TokenizerInfo.from_huggingface(tok, vocab_size=modelo.config.vocab_size)
        gramatica = xgr.GrammarCompiler(info).compile_json_schema(json.dumps(ESQUEMA))


# Decodificação restrita: a cada palavra gerada, proíbe as que quebrariam o JSON do ESQUEMA
class SoJSON(LogitsProcessor):
    def __init__(self):
        self.matcher = xgr.GrammarMatcher(gramatica)
        self.mascara = xgr.allocate_token_bitmask(1, modelo.config.vocab_size)
        self.primeiro = True

    def __call__(self, input_ids, scores):
        if not self.primeiro:
            self.matcher.accept_token(int(input_ids[0, -1]))
        self.primeiro = False
        self.matcher.fill_next_token_bitmask(self.mascara)
        # torch_native porque o padrão usa Triton, que não roda no Windows
        xgr.apply_token_bitmask_inplace(scores, self.mascara.to(scores.device), backend="torch_native")
        return scores


# Manda as mensagens para o modelo e devolve o texto que ele escreveu
def gerar(mensagens, restrito=False, max_tokens=512):
    carregar()
    texto = tok.apply_chat_template(mensagens, add_generation_prompt=True, tokenize=False)
    entrada = tok(texto, return_tensors="pt").to(modelo.device)
    extra = {"logits_processor": [SoJSON()]} if restrito else {}
    saida = modelo.generate(**entrada, max_new_tokens=max_tokens, do_sample=False, **extra)
    return tok.decode(saida[0][entrada.input_ids.shape[1]:], skip_special_tokens=True)


# Lê o JSON da resposta, ou devolve None se o modelo não respondeu no formato certo
def ler_json(texto):
    m = re.search(r"\{.*\}", texto, re.S)
    try:
        d = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None
    ok = (isinstance(d, dict) and isinstance(d.get("resposta"), str) and isinstance(d.get("fontes"), list)
          and isinstance(d.get("recusou"), bool) and isinstance(d.get("confianca"), (int, float)))
    return d if ok else None


# Monta a resposta de recusa ("não consta") no formato do JSON
def recusa(texto):
    return {"resposta": texto, "fontes": [], "recusou": True, "confianca": 0.0}


# Responde a pergunta: A = só o modelo | B = RAG | C = RAG + dados da luminária + JSON garantido (a do site)
def responder(pergunta, config="C"):
    inicio = time.time()
    achados, linha, saida = [], None, None
    placa = achar_placa(pergunta) if config == "C" else None

    if config == "A":
        msgs = [{"role": "system", "content": PROMPT_SEM_CONTEXTO}, {"role": "user", "content": pergunta}]
    else:
        achados = buscar(pergunta)
        contexto = [f"[{d['titulo']}] {d['texto']}" for d, _ in achados]
        if placa:
            linha = consultar_fila(placa)
            if linha:
                contexto.insert(0, f"[Fila de manutenção LuzViva] {texto_fila(linha)}")
            else:
                saida = recusa(f"{RECUSA} A placa {placa} não está na fila.")
        elif achados[0][1] < LIMIAR:  # nenhum trecho parecido o bastante: nem chama o modelo
            saida = recusa(RECUSA)
        msgs = [{"role": "system", "content": PROMPT_RAG},
                {"role": "user", "content": "CONTEXTO:\n" + "\n".join(contexto) + f"\n\nPERGUNTA: {pergunta}"}]

    if saida is not None:
        bruto = json.dumps(saida, ensure_ascii=False)
    else:
        bruto = gerar(msgs, restrito=(config == "C"))
        saida = ler_json(bruto)
        if saida and config != "A":
            # a confiança vem da busca (ou 1.0 com dados da luminária), não do chute do modelo
            saida["confianca"] = 1.0 if linha else round(achados[0][1], 2)

    return {"saida": saida, "bruto": bruto, "latencia": round(time.time() - inicio, 2),
            "nota_busca": round(achados[0][1], 3) if achados else None, "placa": placa}


# ---------- site ----------
app = FastAPI(title="LuzViva IA")


# Formato do que o site manda para /perguntar
class Pergunta(BaseModel):
    pergunta: str


# Abre a página do site
@app.get("/")
def site():
    return FileResponse(f"{PASTA}/index.html")


# Lista as luminárias da mais urgente para a menos urgente (tabela do site)
@app.get("/luminarias")
def listar_luminarias():
    return sorted(luminarias, key=lambda l: -l["prioridade"])


# Recebe a pergunta do site e devolve a resposta (a pergunta não é salva em lugar nenhum, LGPD)
@app.post("/perguntar")
def perguntar(p: Pergunta):
    return responder(p.pergunta)["saida"] or recusa(RECUSA)
