# Render free deploy - sob setting browser panel theke

## Ekbar Render Environment e dite hobe (4 ta)
`API_ID`, `API_HASH`, `WEBHOOK_SECRET` (lomba random text), `RENDER_API_KEY`
RENDER_API_KEY: Render -> Account Settings -> API Keys -> Create API Key. (Eta account er full access, secret rakho.)

## Tarpor sob panel e
1. Service URL kholo -> `WEBHOOK_SECRET` diye Unlock.
2. Step 1: phone + Telegram code (+2FA).
3. Step 2: channel select, optional keys, performance.
4. Step 3: Save and launch -> panel nijei Render env e sob save kore redeploy kore. 1-2 min por bot chalu.

Setting poriborton korte: Render Environment e `FORCE_SETUP=1` dao -> panel abar khulbe (save korle FORCE_SETUP nijei muche jay).
Free plan: 512 MB RAM, idle e sleep, disk nai. UptimeRobot e `/health` ping dao.

## Redeploy chhara live setting (Control panel)
Bot chalu thakle `https://YOUR-SERVICE.onrender.com/admin` kholo (password = `WEBHOOK_SECRET`):
- Target channel bodlano (dropdown ba @username / id)
- Source link on/off, Comments on/off, Comment count, Original caption, Bangla translation, Max comments
Sob change shathe shathe kaj kore, redeploy/restart lage na. Setting gulo tomar Telegram **Saved Messages** e `#zeba_config`
naame ekta message e save hoy (oti **delete koro na**), tai Render restart/redeploy hole-o thake.
Telegram theke-o hoy: `.settings`, `.target @channel`, `.target list`, `.source on|off`, `.comments on|off|10`.
Environment er `TARGET_CHANNEL` etc ekhon shudhu default; panel e bodlale panel er value jite.
Worker count, API key, cookies ityadi start-up setting: egulo Render Environment e bodlate hobe.

## Onek Telegram account (control bot theke)
Control bot e `/login` dile aro userbot account add hoy (phone -> code -> 2FA). Code ta **space diye** pathao: `1 2 3 4 5`.
Login hole bot ekta **session key** dey. `/accounts` e list + Logout button, `/logout 2` dile account ti ei bot e bondho hoy (Telegram session
cholu thake), tarpor `/resume <key>` dile **code chhara** abar login hoy. `/terminate 2` dile Telegram session sotti-i shesh, key r kaj kore na.
Render free e disk nai, tai restart er por account gulo ferot pete Environment e `EXTRA_SESSIONS=key1,key2` dao
(na dile restart er por `/resume <key>` diye abar dao). Key = account er full chabi, kauke dio na.
