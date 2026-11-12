# Piloto de julgamento web do AlphaDive

O piloto inclui quatro tarefas fictícias com seis recortes. A revisão de horários registra os julgamentos no SQLite de teste. As revisões das células alteram a planilha fictícia **AlphaDive - Piloto fictício.xlsx**. O app cria ambos em `training_data/review-web-pilot` ao conectar. A fila de produção do AlphaDive usa outro banco e outra planilha.

## Publicar no Render

1. Coloque `render.yaml` e a pasta `review_web` na raiz de um repositório privado com acesso pelo Render. A publicação precisa somente do código, dos arquivos estáticos e dos PNGs fictícios que acompanham o projeto.
2. Crie um cliente OAuth **Aplicativo da Web** no Google Cloud, com a conta revisora na lista de usuários de teste se o aplicativo estiver nesse modo. Cadastre `https://<endereço-do-piloto>/auth/google/callback` e `http://127.0.0.1:18780/callback` como endereços de retorno. A autenticação do revisor usa `openid email`.
3. Na instalação desktop, execute `runtime\python.exe -m tools.review_drive_setup` e acesse `http://127.0.0.1:18780`. Informe o cliente OAuth web e autorize a criação da pasta dedicada. Essa etapa usa `drive.file` e fornece o ID da pasta, a credencial de gravação, a lista de revisores e a credencial do desktop.
4. Em **New Blueprint**, selecione o repositório e confirme **Free** para o serviço web. Informe as variáveis da tabela. O Blueprint gera o segredo da sessão e o serviço grava arquivos JSON na pasta dedicada.

| Variável | Valor |
| --- | --- |
| `REVIEW_GOOGLE_ID` | ID do cliente OAuth web |
| `REVIEW_GOOGLE_SECRET` | Segredo do cliente OAuth web |
| `REVIEW_GOOGLE_REVIEWERS` | JSON fornecido pelo assistente local, com o identificador Google e o nome de exibição do revisor |
| `REVIEW_DRIVE_FOLDER_ID` | ID da pasta dedicada que a configuração criou no Drive |
| `REVIEW_DRIVE_REFRESH_TOKEN` | Credencial de gravação fornecida pela configuração local |
| `REVIEW_DESKTOP_TOKEN` | Credencial exclusiva para configurar no desktop |

Use a URL que aparecer no painel para finalizar o redirecionamento no Google, se necessário. O serviço usa a URL HTTPS do Render. Para domínio próprio, configure `REVIEW_PUBLIC_URL` com a origem HTTPS e ajuste o redirecionamento no Google. Guarde os segredos nas variáveis do Render e insira a credencial do desktop somente no campo do AlphaDive. Para revogá-la, altere `REVIEW_DESKTOP_TOKEN` no Render e atualize a credencial local.

Confirme `/healthz` e abra a página inicial. O Render pode suspender o serviço gratuito após 15 minutos sem tráfego. Os registros ficam na pasta do Drive e o uso do serviço conta para as franquias do Render. Se a credencial Google estiver no modo de teste e expirar, repita a configuração local e substitua a credencial de gravação no Render. [Render Blueprint](https://render.com/docs/blueprint-spec) · [Limites do plano gratuito](https://render.com/docs/free) · [Escopos do Google Drive](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)

## Rodar o piloto

1. Inicie o AlphaDive atualizado e abra **Fila HITL → Revisão web**. Preencha a URL publicada e a credencial do desktop. Use **Conectar piloto** e aguarde a primeira sincronização.
2. Abra a fila pelo link da seção ou pelo navegador do celular. Entre com uma conta autorizada, transcreva um par de horários e salve uma célula. O estado **Salvo na web** indica que a pasta no Drive recebeu o arquivo de revisão.
3. Abra o AlphaDive novamente ou selecione **Sincronizar agora**. Confira o estado no desktop e na web. A revisão de horário fica no SQLite. A célula revisada aparece na planilha de teste com seu destaque removido.
4. Se a planilha estiver aberta no Excel, salve e feche o arquivo para retomar. Se o valor ou a origem mudou, confira o motivo no desktop. O piloto conserva a revisão remota e o valor local para conferência.

O desktop fica vinculado ao endereço configurado para aquele conjunto de teste. Para usar outro endereço ou reiniciar o piloto do zero, inicie uma instância de teste com um diretório separado em `ALPHADIVE_TRAINING_DIR`. A versão piloto só publica as tarefas e as imagens que correspondem ao conjunto fictício fornecido no código.

## Verificação local

Execute `runtime\python.exe -m unittest discover -s tests -p test_review_web_pilot.py` na raiz do AlphaDive. O teste usa uma pasta JSON local para a fila web, simula a resposta do Google e verifica a gravação no Excel fictício. As credenciais de publicação e o teste em aparelhos móveis fazem parte do aceite da implantação.

## Primeiro teste operacional

Na instalação desktop, use `runtime\python.exe -m tools.review_operational <pasta-do-piloto> <banco-local> <planilha> --preview recortes.png` para preparar a cópia de teste e conferir os recortes localmente. O primeiro lote aceita até cinco grupos de horários e três células numéricas vinculadas à sua origem. Cada tarefa usa um identificador opaco e contém apenas os recortes selecionados e rótulos genéricos.

Para publicar o lote conferido e autorizado, habilite `REVIEW_OPERATIONAL_ENABLED=1` no serviço e execute o mesmo comando com `--publish-to https://<endereço-do-piloto>`. Abra **Teste operacional** na fila web. A seção **Revisão web** do AlphaDive indica a cópia de destino. O julgamento dos horários é registrado no histórico local do teste. As correções numéricas são aplicadas na cópia quando a origem, o recorte e a célula conferem.
