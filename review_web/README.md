# Piloto de julgamento web do AlphaDive

O piloto inclui quatro tarefas fictícias com seis recortes. A revisão de horários registra os julgamentos no SQLite de teste. As revisões das células alteram a planilha fictícia **AlphaDive - Piloto fictício.xlsx**. O app cria ambos em `training_data/review-web-pilot` ao conectar. A fila de produção do AlphaDive usa outro banco e outra planilha.

## Publicar no Render

1. Coloque `render.yaml` e a pasta `review_web` na raiz de um repositório privado com acesso pelo Render. A publicação precisa somente do código, dos arquivos estáticos e dos PNGs fictícios que acompanham o projeto.
2. Crie um cliente OAuth **Aplicativo da Web** no Google Cloud, com os revisores na lista de usuários de teste se o aplicativo estiver nesse modo. Cadastre como URI de redirecionamento `https://<endereço-do-piloto>/auth/google/callback`. O aplicativo usa os escopos `openid email` e permite contas Gmail ou Google Workspace que estiverem na lista do piloto.
3. Em **New Blueprint**, selecione o repositório e confirme **Free** para o serviço web e para o Postgres. Informe as variáveis solicitadas na tabela. O Blueprint gera o segredo da sessão, usa a conexão interna com o Postgres e desabilita o acesso externo direto ao banco.

| Variável | Valor |
| --- | --- |
| `REVIEW_GOOGLE_ID` | ID do cliente OAuth web |
| `REVIEW_GOOGLE_SECRET` | Segredo do cliente OAuth web |
| `REVIEW_GOOGLE_REVIEWERS` | JSON com e-mails em minúsculas e nomes de exibição, como `{"revisor@gmail.com":"Revisor de teste"}` |
| `REVIEW_DESKTOP_TOKEN` | Gere uma credencial exclusiva, por exemplo com `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

Use a URL que aparecer no painel para finalizar o redirecionamento no Google, se necessário. O serviço usa a URL HTTPS do Render. Para domínio próprio, configure `REVIEW_PUBLIC_URL` com a origem HTTPS e ajuste o redirecionamento no Google. Guarde os segredos nas variáveis do Render e insira a credencial do desktop somente no campo do AlphaDive. Para revogá-la, altere `REVIEW_DESKTOP_TOKEN` no Render e atualize a credencial local.

Confirme `/healthz` e abra a página inicial. O Render pode suspender o serviço gratuito após 15 minutos sem tráfego. O banco gratuito expira em 30 dias e o uso conta para as franquias da conta. [Render Blueprint](https://render.com/docs/blueprint-spec) · [Limites do plano gratuito](https://render.com/docs/free) · [Google OpenID Connect](https://developers.google.com/identity/openid-connect/openid-connect)

## Rodar o piloto

1. Inicie o AlphaDive atualizado e abra **Fila HITL → Revisão web**. Preencha a URL publicada e a credencial do desktop. Use **Conectar piloto** e aguarde a primeira sincronização.
2. Abra a fila pelo link da seção ou pelo navegador do celular. Entre com uma conta autorizada, transcreva um par de horários e salve uma célula. O estado **Salvo na web** indica que o banco remoto recebeu a revisão.
3. Abra o AlphaDive novamente ou selecione **Sincronizar agora**. Confira o estado no desktop e na web. A revisão de horário fica no SQLite. A célula revisada aparece na planilha de teste com seu destaque removido.
4. Se a planilha estiver aberta no Excel, salve e feche o arquivo para retomar. Se o valor ou a origem mudou, confira o motivo no desktop. O piloto conserva a revisão remota e o valor local para conferência.

O desktop fica vinculado ao endereço configurado para aquele conjunto de teste. Para usar outro endereço ou reiniciar o piloto do zero, inicie uma instância de teste com um diretório separado em `ALPHADIVE_TRAINING_DIR`. A versão piloto só publica as tarefas e as imagens que correspondem ao conjunto fictício fornecido no código.

## Verificação local

Execute `runtime\python.exe -m unittest discover -s tests -p test_review_web_pilot.py` na raiz do AlphaDive. O teste usa SQLite para a fila web, simula a resposta do Google e verifica a gravação no Excel fictício. O Render usa Postgres e login Google. As credenciais de publicação e o teste em aparelhos móveis fazem parte do aceite da implantação.
