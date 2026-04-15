/**
 * WhatsApp Gateway — bridges WhatsApp Web to the LeadQualBot FastAPI agent.
 * Listens on port 3001.
 *   POST /send  { number: "601234567890", text: "..." }  → send WA message
 * Incoming WA messages → POST ${FASTAPI_BASE_URL}/internal/wa
 */

const { Client, LocalAuth } = require('whatsapp-web.js');
const express = require('express');
const qrcode = require('qrcode');

const app = express();
app.use(express.json());

const FASTAPI_BASE_URL = process.env.FASTAPI_BASE_URL || 'http://127.0.0.1:8080';
const FASTAPI_URL = `${FASTAPI_BASE_URL}/internal/wa`;
const ADMIN_API_TOKEN = process.env.ADMIN_API_TOKEN || '';
const PORT = 3001;

function internalHeaders() {
    const headers = { 'Content-Type': 'application/json' };
    if (ADMIN_API_TOKEN) headers['X-Admin-Token'] = ADMIN_API_TOKEN;
    return headers;
}

let clientReady = false;
let currentQR = null;

// Track message IDs sent by Maya so we can distinguish them from admin manual replies
const _mayaSentIds = new Set();
const SENT_ID_TTL_MS = 90_000; // 90 seconds — enough for any send delay
// Track in-flight sends (registered before sendMessage so message_create can't race ahead)
const _pendingSends = new Set(); // key: `${jid}|${body}`

const client = new Client({
    authStrategy: new LocalAuth({
        dataPath: './auth',
        clientId: 'default',
    }),
    puppeteer: {
        headless: true,
        executablePath: '/usr/bin/chromium-browser',
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--no-first-run',
            '--no-zygote',
            '--single-process',
        ],
    },
});

client.on('qr', async (qr) => {
    currentQR = await qrcode.toDataURL(qr);
    console.log('[WA Gateway] QR ready — open https://agent.steadigital.com/wa-qr to scan.');
});

client.on('authenticated', () => {
    console.log('[WA Gateway] Authenticated.');
});

client.on('ready', () => {
    clientReady = true;
    currentQR = null;
    console.log('[WA Gateway] Ready — WhatsApp connected.');
});

client.on('disconnected', (reason) => {
    clientReady = false;
    console.log('[WA Gateway] Disconnected:', reason, '— reconnecting in 10s...');
    setTimeout(() => client.initialize(), 10000);
});

client.on('message', async (message) => {
    // Skip groups and broadcast lists
    if (message.isGroupMsg || message.from.endsWith('@broadcast')) return;
    if (message.type !== 'chat') return; // text only

    // Use the full JID as sender_id so replies go back to the right address (@c.us or @lid)
    const sender_id = message.from;
    const text = message.body;
    const name = message._data?.notifyName || null;

    console.log(`[WA Gateway] Incoming from ${sender_id}: ${text.substring(0, 60)}`);

    // Show "typing..." while the agent processes
    const chat = await message.getChat();
    await chat.sendStateTyping();

    try {
        const res = await fetch(FASTAPI_URL, {
            method: 'POST',
            headers: internalHeaders(),
            body: JSON.stringify({ sender_id, text, name }),
        });
        if (!res.ok) {
            console.error('[WA Gateway] FastAPI error:', res.status, await res.text());
        }
    } catch (e) {
        console.error('[WA Gateway] Failed to reach FastAPI:', e.message);
    } finally {
        await chat.clearState();
    }
});

// Detect admin manual replies — fires for ALL outgoing messages (both Maya and admin)
client.on('message_create', async (message) => {
    if (!message.fromMe) return;                                   // only outgoing
    if (message.type !== 'chat') return;                           // text only
    if (_mayaSentIds.has(message.id._serialized)) return;          // Maya sent this — skip
    if (_pendingSends.has(`${message.to}|${message.body}`)) {      // in-flight send, race condition
        _mayaSentIds.add(message.id._serialized);
        setTimeout(() => _mayaSentIds.delete(message.id._serialized), SENT_ID_TTL_MS);
        return;
    }
    const to = message.to;
    if (!to || to.endsWith('@broadcast') || to.endsWith('@g.us')) return;

    console.log(`[WA Gateway] Admin reply detected → ${to}: ${(message.body || '').substring(0, 60)}`);
    try {
        await fetch(`${FASTAPI_BASE_URL}/internal/wa/admin-reply`, {
            method: 'POST',
            headers: internalHeaders(),
            body: JSON.stringify({ recipient_id: to, text: message.body || '' }),
        });
    } catch (e) {
        console.error('[WA Gateway] Failed to report admin reply:', e.message);
    }
});

// HTTP endpoint — FastAPI calls this to send a WA message
app.post('/send', async (req, res) => {
    const { number, text } = req.body;
    if (!number || !text) {
        return res.status(400).json({ error: 'number and text required' });
    }
    if (!clientReady) {
        return res.status(503).json({ error: 'WhatsApp client not ready' });
    }
    try {
        const chunks = [];
        for (let i = 0; i < text.length; i += 4000) {
            chunks.push(text.substring(i, i + 4000));
        }
        // number may already be a full JID (e.g. "123@lid") or just digits
        const jid = number.includes('@') ? number : `${number}@c.us`;
        // getChatById does not work for @lid JIDs — use sendMessage directly for those
        const isLid = jid.endsWith('@lid');
        const chat = isLid ? null : await client.getChatById(jid).catch(() => null);
        for (const chunk of chunks) {
            // Show typing indicator, pause proportional to reply length (1–4 seconds)
            if (chat) await chat.sendStateTyping();
            const delay = Math.min(1500 + chunk.length * 18, 4000);
            await new Promise(r => setTimeout(r, delay));
            if (chat) await chat.clearState();
            // Register pending send BEFORE sendMessage so message_create can't race ahead
            const pendingKey = `${jid}|${chunk}`;
            _pendingSends.add(pendingKey);
            const sent = await client.sendMessage(jid, chunk);
            _pendingSends.delete(pendingKey);
            // Register ID so message_create listener knows this was Maya, not admin
            if (sent?.id?._serialized) {
                _mayaSentIds.add(sent.id._serialized);
                setTimeout(() => _mayaSentIds.delete(sent.id._serialized), SENT_ID_TTL_MS);
            }
        }
        res.json({ status: 'ok' });
    } catch (e) {
        console.error('[WA Gateway] Send error:', e.message);
        res.status(500).json({ error: e.message });
    }
});

app.get('/health', (req, res) => {
    res.json({ ready: clientReady, qr_pending: !!currentQR });
});

app.get('/qr', (req, res) => {
    if (clientReady) {
        return res.send('<h2>WhatsApp already connected!</h2>');
    }
    if (!currentQR) {
        return res.send('<h2>QR not ready yet — refresh in a few seconds...</h2><meta http-equiv="refresh" content="3">');
    }
    res.send(`<!DOCTYPE html><html><head><title>Scan QR</title>
<meta http-equiv="refresh" content="30">
<style>body{font-family:sans-serif;text-align:center;padding:40px;background:#f0f0f0;}
img{max-width:300px;border:2px solid #333;border-radius:8px;}</style></head>
<body><h2>Scan with WhatsApp</h2>
<p>Settings → Linked Devices → Link a Device</p>
<img src="${currentQR}">
<p><small>Auto-refreshes every 30s</small></p>
</body></html>`);
});

app.listen(PORT, () => {
    console.log(`[WA Gateway] HTTP server on port ${PORT}`);
});

client.initialize();
