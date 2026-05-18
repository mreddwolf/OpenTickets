# ToyBox Ticket Bot Community

Versao publica de portfolio de um bot de tickets para Discord.

O objetivo deste projeto e demonstrar uma implementacao real de atendimento em Discord usando `discord.py`: painel com dropdown, modal de abertura, threads privadas, historico por usuario, status de atendimento e configuracao por slash command.

## Recursos

- Painel configuravel com `/setup`.
- Dropdown com categorias de ticket e consulta de historico.
- Modal com `Assunto` e `Relato`.
- Threads privadas para cada ticket.
- Nome da thread com status visual.
- Historico do usuario com `/meustickets` ou pela opcao `Meus tickets`.
- Informacoes internas para equipe com `/ticketinfo`.
- Controle basico contra abuso de abertura de tickets.

## Status Da Thread

```text
🔵 ticket-00001 | Usuario | Aberto
🟢 ticket-00001 | Usuario | Em Atendimento
🟠 ticket-00001 | Usuario | Concluido
🔴 ticket-00001 | Usuario | Fechado
```

## Instalar

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Crie um arquivo `.env` baseado em `.env.example`:

```env
DISCORD_TOKEN=coloque_o_token_aqui
DISCORD_GUILD_ID=123456789012345678
DISCORD_SUPPORT_ROLE_ID=123456789012345678
TICKET_RATE_LIMIT_MAX=3
TICKET_RATE_LIMIT_WINDOW_SECONDS=3600
TICKET_RATE_LIMIT_COOLDOWN_SECONDS=60
```

Inicie:

```powershell
python main.py
```

## Comandos

- `/setup`: configura e publica o painel.
- `/atenderticket`: marca o ticket como em atendimento.
- `/concluirticket`: marca o ticket como concluido.
- `/fecharticket`: fecha e arquiva o ticket.
- `/meustickets`: mostra o historico do usuario.
- `/ticketinfo`: mostra informacoes internas do ticket para a equipe.

## Dados Locais

O bot cria automaticamente:

- `config.json`
- `tickets.json`

Esses arquivos nao devem ser enviados ao GitHub. Eles ficam no `.gitignore`.

## Nota De Portfolio

Esta versao e uma demonstracao publica. Para produto comercial, recomenda-se evoluir persistencia, logs, transcripts, deploy, multi-servidor e painel administrativo.
