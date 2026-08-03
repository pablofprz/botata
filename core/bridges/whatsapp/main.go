// bridge de WhatsApp para Botata (T41).
//
// WhatsApp no se puede hablar desde Python: el número entra como DISPOSITIVO
// VINCULADO (el mismo mecanismo que WhatsApp Web) y eso lo resuelve whatsmeow,
// que es Go. Este proceso es la única pieza que habla ese protocolo; el motor
// le pega por HTTP en localhost.
//
// El contrato está fijado en core/src/channels.py (WhatsAppChannel) y testeado
// contra un bridge falso, así que este archivo tiene que cumplirlo y nada más:
//
//	GET  /status              → {"connected", "qr", "me": {"id","name"}}
//	GET  /qr.png              → el QR vigente como imagen (para la UI)
//	GET  /messages?after=<c>  → {"cursor", "messages": [...]}
//	GET  /messages/<id>       → un mensaje ya visto
//	POST /send                → {"chat_id","text","reply_to"?,"media_path"?}
//	                            (media_path: imagen, o audio por extensión —
//	                            .ogg/.opus sale como nota de voz)
//	POST /profile             → {"status"}
//	GET  /groups              → grupos con su JID (para llenar WHATSAPP_CHAT_IDS)
//
// Escucha SOLO en loopback: quien llegue a este puerto puede escribir en el
// WhatsApp del número, así que no se expone a la red.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	qrcode "github.com/skip2/go-qrcode"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"

	_ "modernc.org/sqlite" // driver de SQLite en Go puro: sin cgo, sin gcc en Windows
)

// ─── El buffer de mensajes ─────────────────────────────────────────────────
// El motor lee por poll con un cursor, así que acá hace falta una cola. Es un
// anillo en memoria y no una tabla: si el bridge se reinicia, el motor relee
// desde el vivo y el dedup lo hace la DB de Botata (has_replied). Persistir
// esto sería duplicar una fuente de verdad que ya existe.

const maxBuffer = 2000

type media struct {
	Path     string `json:"path,omitempty"`
	Mime     string `json:"mime"`
	Filename string `json:"filename,omitempty"`
}

type mensaje struct {
	Seq            int64    `json:"-"`
	ID             string   `json:"id"`
	ChatID         string   `json:"chat_id"`
	ChatName       string   `json:"chat_name"`
	AuthorID       string   `json:"author_id"`
	AuthorName     string   `json:"author_name"`
	Text           string   `json:"text"`
	QuotedID       *string  `json:"quoted_id"`
	QuotedAuthorID *string  `json:"quoted_author_id"`
	Mentions       []string `json:"mentions"`
	Media          []media  `json:"media"`
	FromMe         bool     `json:"from_me"`
	TS             int64    `json:"ts"`
}

type buffer struct {
	mu    sync.RWMutex
	items []mensaje
	seq   int64
}

func (b *buffer) push(m mensaje) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.seq++
	m.Seq = b.seq
	b.items = append(b.items, m)
	if len(b.items) > maxBuffer {
		b.items = b.items[len(b.items)-maxBuffer:]
	}
}

func (b *buffer) desde(after int64) ([]mensaje, int64) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	out := make([]mensaje, 0, 16)
	for _, m := range b.items {
		if m.Seq > after {
			out = append(out, m)
		}
	}
	return out, b.seq
}

func (b *buffer) porID(id string) (mensaje, bool) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	for i := len(b.items) - 1; i >= 0; i-- {
		if b.items[i].ID == id {
			return b.items[i], true
		}
	}
	return mensaje{}, false
}

// ─── Estado global del bridge ──────────────────────────────────────────────

type puente struct {
	cli      *whatsmeow.Client
	buf      buffer
	mediaDir string
	// Chats que el bot atiende. Vacío = todos (hace falta al vincular, cuando
	// todavía no se sabe el JID de ningún grupo). Con lista, lo de afuera se
	// descarta ACÁ: un dispositivo vinculado recibe todos los mensajes del
	// número, y sin este filtro el bridge bajaba a disco las fotos de las
	// conversaciones personales del dueño para que después Python las tirara.
	chats map[string]bool

	mu sync.RWMutex
	qr string // QR vigente mientras no esté vinculado ("" si ya lo está)
}

func (p *puente) atiende(chat string) bool {
	return len(p.chats) == 0 || p.chats[chat]
}

func (p *puente) setQR(s string) {
	p.mu.Lock()
	p.qr = s
	p.mu.Unlock()
}

func (p *puente) getQR() string {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.qr
}

// ─── Traducción de un mensaje de WhatsApp a la forma del contrato ──────────

func textoDe(m *waE2E.Message) string {
	if m == nil {
		return ""
	}
	if t := m.GetConversation(); t != "" {
		return t
	}
	if e := m.GetExtendedTextMessage(); e != nil {
		return e.GetText()
	}
	// Los captions cuentan como texto: un meme con epígrafe es un mensaje.
	if i := m.GetImageMessage(); i != nil {
		return i.GetCaption()
	}
	if v := m.GetVideoMessage(); v != nil {
		return v.GetCaption()
	}
	if d := m.GetDocumentMessage(); d != nil {
		return d.GetCaption()
	}
	return ""
}

func contextoDe(m *waE2E.Message) *waE2E.ContextInfo {
	if m == nil {
		return nil
	}
	if e := m.GetExtendedTextMessage(); e != nil {
		return e.GetContextInfo()
	}
	if i := m.GetImageMessage(); i != nil {
		return i.GetContextInfo()
	}
	if v := m.GetVideoMessage(); v != nil {
		return v.GetContextInfo()
	}
	return nil
}

// bajarMedia guarda el adjunto en disco y devuelve su descripción. Se baja acá
// y no en Python porque la media de WhatsApp viene cifrada: la clave la tiene
// esta sesión. El motor solo ve un path local.
func (p *puente) bajarMedia(ctx context.Context, m *waE2E.Message, id string) []media {
	if m == nil {
		return nil
	}
	var (
		desc      whatsmeow.DownloadableMessage
		mime, ext string
	)
	switch {
	case m.GetImageMessage() != nil:
		im := m.GetImageMessage()
		desc, mime, ext = im, im.GetMimetype(), ".jpg"
	case m.GetVideoMessage() != nil:
		vm := m.GetVideoMessage()
		desc, mime, ext = vm, vm.GetMimetype(), ".mp4"
	case m.GetAudioMessage() != nil:
		am := m.GetAudioMessage()
		desc, mime, ext = am, am.GetMimetype(), ".ogg"
	case m.GetStickerMessage() != nil:
		sm := m.GetStickerMessage()
		desc, mime, ext = sm, sm.GetMimetype(), ".webp"
	default:
		return nil
	}
	datos, err := p.cli.Download(ctx, desc)
	if err != nil {
		log.Printf("no pude bajar la media de %s: %v", id, err)
		// Se anota igual: el bot tiene que saber que había una imagen aunque no
		// la pueda mirar. Perder el hecho es peor que perder el archivo.
		return []media{{Mime: mime}}
	}
	nombre := id + ext
	ruta := filepath.Join(p.mediaDir, nombre)
	if err := os.WriteFile(ruta, datos, 0o600); err != nil {
		log.Printf("no pude guardar %s: %v", ruta, err)
		return []media{{Mime: mime}}
	}
	return []media{{Path: ruta, Mime: mime, Filename: nombre}}
}

// telefono devuelve la identidad de alguien como número de teléfono.
//
// Los grupos nuevos direccionan por LID (`104913921159305@lid`): un id opaco,
// distinto del número, que NO se ve por ningún lado desde el teléfono. Si el
// bridge lo publicara tal cual, nadie podría escribir a mano un ADMIN_HANDLE ni
// un grupo de usuarios — el admin solo puede escribir lo que puede ver.
//
// `alt` es la contraparte que a veces manda el propio evento (SenderAlt); si no
// está, se busca el mapeo LID→PN que whatsmeow guarda en la sesión. Si no hay
// forma, vuelve el LID: es peor perder al autor que devolver un id feo.
func (p *puente) telefono(ctx context.Context, jid types.JID, alt types.JID) types.JID {
	if jid.Server != types.HiddenUserServer {
		return jid.ToNonAD()
	}
	if !alt.IsEmpty() && alt.Server == types.DefaultUserServer {
		return alt.ToNonAD()
	}
	if pn, err := p.cli.Store.LIDs.GetPNForLID(ctx, jid.ToNonAD()); err == nil && !pn.IsEmpty() {
		return pn.ToNonAD()
	}
	return jid.ToNonAD()
}

func (p *puente) telefonoStr(ctx context.Context, raw string) string {
	jid, err := types.ParseJID(raw)
	if err != nil {
		return raw
	}
	return p.telefono(ctx, jid, types.EmptyJID).String()
}

func (p *puente) onMessage(ctx context.Context, evt *events.Message) {
	// Antes de tocar nada: si el chat no es del bot, el mensaje no existe. En
	// particular no se baja su media — bajarla primero y descartarla después
	// dejaba fotos de chats personales en el disco de la instancia.
	if !p.atiende(evt.Info.Chat.String()) {
		return
	}
	ctxInfo := contextoDe(evt.Message)
	var quotedID, quotedAutor *string
	if ctxInfo != nil && ctxInfo.GetStanzaID() != "" {
		q := ctxInfo.GetStanzaID()
		quotedID = &q
		if a := ctxInfo.GetParticipant(); a != "" {
			autor := p.telefonoStr(ctx, a)
			quotedAutor = &autor
		}
	}
	menciones := []string{}
	if ctxInfo != nil {
		for _, m := range ctxInfo.GetMentionedJID() {
			menciones = append(menciones, p.telefonoStr(ctx, m))
		}
	}
	nombreChat := evt.Info.Chat.String()
	if info, err := p.cli.GetGroupInfo(ctx, evt.Info.Chat); err == nil && info != nil {
		nombreChat = info.Name
	}
	p.buf.push(mensaje{
		ID:             evt.Info.ID,
		ChatID:         evt.Info.Chat.String(),
		ChatName:       nombreChat,
		AuthorID:       p.telefono(ctx, evt.Info.Sender, evt.Info.SenderAlt).String(),
		AuthorName:     evt.Info.PushName,
		Text:           textoDe(evt.Message),
		QuotedID:       quotedID,
		QuotedAuthorID: quotedAutor,
		Mentions:       menciones,
		Media:          p.bajarMedia(ctx, evt.Message, evt.Info.ID),
		FromMe:         evt.Info.IsFromMe,
		TS:             evt.Info.Timestamp.Unix(),
	})
}

// ─── HTTP ──────────────────────────────────────────────────────────────────

func escribirJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if v != nil {
		_ = json.NewEncoder(w).Encode(v)
	}
}

func (p *puente) handleStatus(w http.ResponseWriter, _ *http.Request) {
	out := map[string]any{"connected": false, "qr": nil, "me": nil}
	if qr := p.getQR(); qr != "" {
		out["qr"] = qr
	}
	if id := p.cli.Store.ID; id != nil {
		out["connected"] = p.cli.IsConnected()
		me := map[string]string{
			"id":   id.ToNonAD().String(),
			"name": p.cli.Store.PushName,
		}
		// El bot se ve a sí mismo con DOS identidades: su número y su LID. En un
		// grupo direccionado por LID, una cita a un mensaje suyo viene firmada
		// con el LID — si el canal solo conoce el número, no reconoce que le
		// están contestando y se queda mudo.
		if lid := p.cli.Store.LID; !lid.IsEmpty() {
			me["lid"] = lid.ToNonAD().String()
		}
		out["me"] = me
	}
	escribirJSON(w, 200, out)
}

// handleQRPNG sirve el QR vigente como imagen. Vincular es el único paso de
// configuración que no es escribir en un campo — hay que escanear — y un QR es
// una imagen, no un string: la UI no puede dibujarlo sola sin meterle una
// librería al panel (que es stdlib puro a propósito). El QR vive acá, así que
// se dibuja acá. No se cachea: rota cada ~30s.
func (p *puente) handleQRPNG(w http.ResponseWriter, _ *http.Request) {
	qr := p.getQR()
	if qr == "" {
		escribirJSON(w, 404, map[string]string{"error": "no hay QR vigente (¿ya está vinculado?)"})
		return
	}
	// Tamaño negativo = píxeles POR MÓDULO (no ancho total): así los módulos
	// caen en píxeles enteros y no quedan bordes a medio módulo, que es lo que
	// hace que una cámara no enganche.
	png, err := qrcode.Encode(qr, qrcode.Low, -8)
	if err != nil {
		escribirJSON(w, 500, map[string]string{"error": err.Error()})
		return
	}
	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(png)
}

func (p *puente) handleMessages(w http.ResponseWriter, r *http.Request) {
	if id := strings.TrimPrefix(r.URL.Path, "/messages/"); id != "" && id != r.URL.Path {
		if m, ok := p.buf.porID(id); ok {
			escribirJSON(w, 200, m)
		} else {
			escribirJSON(w, 404, nil)
		}
		return
	}
	after, _ := strconv.ParseInt(r.URL.Query().Get("after"), 10, 64)
	msgs, cursor := p.buf.desde(after)
	escribirJSON(w, 200, map[string]any{
		"cursor": strconv.FormatInt(cursor, 10), "messages": msgs})
}

// Extensión → mimetype de audio. Decide también el modo de upload en /send:
// lo que matchea acá sube como MediaAudio; el resto sigue siendo imagen.
var mimeAudio = map[string]string{
	".ogg":  "audio/ogg; codecs=opus",
	".opus": "audio/ogg; codecs=opus",
	".mp3":  "audio/mpeg",
	".m4a":  "audio/mp4",
}

type envio struct {
	ChatID    string `json:"chat_id"`
	Text      string `json:"text"`
	ReplyTo   string `json:"reply_to"`
	MediaPath string `json:"media_path"`
}

func (p *puente) handleSend(w http.ResponseWriter, r *http.Request) {
	var req envio
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		escribirJSON(w, 400, map[string]string{"error": "json inválido: " + err.Error()})
		return
	}
	jid, err := types.ParseJID(req.ChatID)
	if err != nil {
		escribirJSON(w, 400, map[string]string{"error": "chat_id inválido: " + req.ChatID})
		return
	}
	ctx := r.Context()
	var ctxInfo *waE2E.ContextInfo
	if req.ReplyTo != "" {
		ctxInfo = &waE2E.ContextInfo{StanzaID: proto.String(req.ReplyTo)}
		if orig, ok := p.buf.porID(req.ReplyTo); ok {
			ctxInfo.Participant = proto.String(orig.AuthorID)
			ctxInfo.QuotedMessage = &waE2E.Message{Conversation: proto.String(orig.Text)}
		}
	}

	msg := &waE2E.Message{}
	if req.MediaPath != "" {
		datos, err := os.ReadFile(req.MediaPath)
		if err != nil {
			// Mandar el texto sin la imagen es mejor que no mandar nada: el bot
			// ya decidió qué decir.
			log.Printf("no pude leer %s (%v) — mando solo el texto", req.MediaPath, err)
			req.MediaPath = ""
		} else if mime, esAudio := mimeAudio[strings.ToLower(filepath.Ext(req.MediaPath))]; esAudio {
			up, err := p.cli.Upload(ctx, datos, whatsmeow.MediaAudio)
			if err != nil {
				log.Printf("upload de audio falló (%v) — mando solo el texto", err)
				req.MediaPath = ""
			} else {
				// AudioMessage no tiene caption: si el bot escribió algo, va en
				// un mensaje aparte ANTES, y la nota de voz llega pegada atrás.
				// Si ese texto falla, el audio sale igual — es lo que pidieron.
				if req.Text != "" {
					txt := &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
						Text: proto.String(req.Text), ContextInfo: ctxInfo}}
					if _, err := p.cli.SendMessage(ctx, jid, txt); err != nil {
						log.Printf("no pude mandar el texto que acompaña al audio: %v", err)
					}
				}
				msg.AudioMessage = &waE2E.AudioMessage{
					// PTT = nota de voz (burbuja con play); solo vale con
					// ogg/opus. Un mp3 sale como archivo de audio común.
					PTT:           proto.Bool(strings.HasPrefix(mime, "audio/ogg")),
					Mimetype:      proto.String(mime),
					URL:           proto.String(up.URL),
					DirectPath:    proto.String(up.DirectPath),
					MediaKey:      up.MediaKey,
					FileEncSHA256: up.FileEncSHA256,
					FileSHA256:    up.FileSHA256,
					FileLength:    proto.Uint64(up.FileLength),
					ContextInfo:   ctxInfo,
				}
			}
		} else {
			up, err := p.cli.Upload(ctx, datos, whatsmeow.MediaImage)
			if err != nil {
				log.Printf("upload falló (%v) — mando solo el texto", err)
				req.MediaPath = ""
			} else {
				msg.ImageMessage = &waE2E.ImageMessage{
					Caption:       proto.String(req.Text),
					Mimetype:      proto.String("image/jpeg"),
					URL:           proto.String(up.URL),
					DirectPath:    proto.String(up.DirectPath),
					MediaKey:      up.MediaKey,
					FileEncSHA256: up.FileEncSHA256,
					FileSHA256:    up.FileSHA256,
					FileLength:    proto.Uint64(up.FileLength),
					ContextInfo:   ctxInfo,
				}
			}
		}
	}
	if msg.ImageMessage == nil && msg.AudioMessage == nil {
		msg.ExtendedTextMessage = &waE2E.ExtendedTextMessage{
			Text: proto.String(req.Text), ContextInfo: ctxInfo}
	}

	resp, err := p.cli.SendMessage(ctx, jid, msg)
	if err != nil {
		escribirJSON(w, 502, map[string]string{"error": err.Error()})
		return
	}
	escribirJSON(w, 200, map[string]string{"id": resp.ID})
}

// handleReact: "me gusta" del bot sobre un mensaje ajeno. WhatsApp no tiene
// like: el gesto equivalente es una reacción, que se firma contra (chat, autor,
// id del mensaje) — por eso el canal manda también el author_id.
func (p *puente) handleReact(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ChatID    string `json:"chat_id"`
		MessageID string `json:"message_id"`
		AuthorID  string `json:"author_id"`
		Emoji     string `json:"emoji"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		escribirJSON(w, 400, map[string]string{"error": "json inválido: " + err.Error()})
		return
	}
	chat, err := types.ParseJID(req.ChatID)
	if err != nil {
		escribirJSON(w, 400, map[string]string{"error": "chat_id inválido: " + req.ChatID})
		return
	}
	autor := req.AuthorID
	if autor == "" {
		if orig, ok := p.buf.porID(req.MessageID); ok {
			autor = orig.AuthorID
		}
	}
	emisor, err := types.ParseJID(autor)
	if err != nil {
		escribirJSON(w, 400, map[string]string{"error": "no sé quién escribió " + req.MessageID})
		return
	}
	emoji := req.Emoji
	if emoji == "" {
		emoji = "❤️"
	}
	msg := p.cli.BuildReaction(chat, emisor, req.MessageID, emoji)
	if _, err := p.cli.SendMessage(r.Context(), chat, msg); err != nil {
		escribirJSON(w, 502, map[string]string{"error": err.Error()})
		return
	}
	escribirJSON(w, 204, nil)
}

func (p *puente) handleProfile(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Status string `json:"status"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		escribirJSON(w, 400, nil)
		return
	}
	if err := p.cli.SetStatusMessage(r.Context(), req.Status); err != nil {
		escribirJSON(w, 502, map[string]string{"error": err.Error()})
		return
	}
	escribirJSON(w, 204, nil)
}

// handleGroups existe para un problema concreto de configuración: el JID de un
// grupo no se ve por ningún lado desde el teléfono, y sin él no se puede
// llenar WHATSAPP_CHAT_IDS.
func (p *puente) handleGroups(w http.ResponseWriter, r *http.Request) {
	grupos, err := p.cli.GetJoinedGroups(r.Context())
	if err != nil {
		escribirJSON(w, 502, map[string]string{"error": err.Error()})
		return
	}
	out := make([]map[string]any, 0, len(grupos))
	for _, g := range grupos {
		out = append(out, map[string]any{
			"id": g.JID.String(), "name": g.Name, "participants": len(g.Participants)})
	}
	escribirJSON(w, 200, map[string]any{"groups": out})
}

// ─── main ──────────────────────────────────────────────────────────────────

func main() {
	// Fuera del rango de la UI de config (8787 + hasta 20 puertos si están
	// ocupados): los dos procesos corren a la vez mientras se configura el
	// canal, y si la UI le gana el puerto al bridge, WHATSAPP_BRIDGE_URL
	// apunta a la UI y el error no se entiende.
	addr := flag.String("addr", "127.0.0.1:8899", "dónde escuchar (solo loopback)")
	datos := flag.String("data", ".", "carpeta de la sesión y la media bajada")
	chats := flag.String("chats", "", "JIDs que el bot atiende, separados por coma "+
		"(los mismos de WHATSAPP_CHAT_IDS). Vacío = todos, que es lo que hace "+
		"falta recién vinculado para poder listar los grupos")
	flag.Parse()

	// Absoluto, no relativo: el path de la media viaja al motor, que corre en
	// OTRO directorio (y en prod puede ser otro proceso arrancado por systemd).
	// Con `-data ../../../bots/x` el bridge publicaba paths que solo existían
	// desde su propio cwd, así que el motor no encontraba el archivo, no podía
	// mirarlo y el bot contestaba como si la imagen no estuviera.
	absDatos, err := filepath.Abs(*datos)
	if err != nil {
		log.Fatalf("no pude resolver -data %q: %v", *datos, err)
	}
	*datos = absDatos
	if err := os.MkdirAll(filepath.Join(*datos, "media"), 0o700); err != nil {
		log.Fatalf("no pude crear la carpeta de datos: %v", err)
	}

	ctx := context.Background()
	logger := waLog.Stdout("whatsmeow", "WARN", true)
	dsn := fmt.Sprintf("file:%s?_pragma=foreign_keys(1)&_pragma=busy_timeout(5000)",
		filepath.Join(*datos, "session.db"))
	contenedor, err := sqlstore.New(ctx, "sqlite", dsn, logger)
	if err != nil {
		log.Fatalf("no pude abrir la sesión: %v", err)
	}
	device, err := contenedor.GetFirstDevice(ctx)
	if err != nil {
		log.Fatalf("no pude leer el dispositivo: %v", err)
	}

	permitidos := map[string]bool{}
	for _, c := range strings.Split(*chats, ",") {
		if c = strings.TrimSpace(c); c != "" {
			permitidos[c] = true
		}
	}
	if len(permitidos) == 0 {
		log.Printf("⚠ sin -chats: se procesan TODOS los chats del número, incluidas "+
			"las conversaciones personales (y se baja su media a %s). Pasá -chats "+
			"con los JIDs del bot apenas los tengas.", filepath.Join(*datos, "media"))
	} else {
		log.Printf("atendiendo %d chat(s): %s", len(permitidos), *chats)
	}

	p := &puente{cli: whatsmeow.NewClient(device, logger),
		mediaDir: filepath.Join(*datos, "media"), chats: permitidos}
	p.cli.AddEventHandler(func(evt any) {
		if m, ok := evt.(*events.Message); ok {
			p.onMessage(ctx, m)
		}
	})

	if p.cli.Store.ID == nil {
		// Sin sesión: hay que vincular. El QR sale por /status (lo muestra la
		// UI de Botata) y también por acá, para el que esté mirando la consola.
		qrChan, err := p.cli.GetQRChannel(ctx)
		if err != nil {
			log.Fatalf("no pude pedir el QR: %v", err)
		}
		if err := p.cli.Connect(); err != nil {
			log.Fatalf("no pude conectar: %v", err)
		}
		go func() {
			for evt := range qrChan {
				switch evt.Event {
				case "code":
					p.setQR(evt.Code)
					log.Printf("escaneá este QR desde WhatsApp → Ajustes → "+
						"Dispositivos vinculados (o miralo en la UI de Botata):\n%s",
						evt.Code)
				case "success":
					p.setQR("")
					log.Printf("vinculado")
				default:
					log.Printf("QR: %s", evt.Event)
				}
			}
		}()
	} else if err := p.cli.Connect(); err != nil {
		log.Fatalf("no pude conectar: %v", err)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/status", p.handleStatus)
	mux.HandleFunc("/qr.png", p.handleQRPNG)
	mux.HandleFunc("/messages", p.handleMessages)
	mux.HandleFunc("/messages/", p.handleMessages)
	mux.HandleFunc("/send", p.handleSend)
	mux.HandleFunc("/react", p.handleReact)
	mux.HandleFunc("/profile", p.handleProfile)
	mux.HandleFunc("/groups", p.handleGroups)

	if !strings.HasPrefix(*addr, "127.0.0.1:") && !strings.HasPrefix(*addr, "localhost:") {
		// Quien llegue a este puerto escribe en el WhatsApp del número. No es
		// un servicio para exponer.
		log.Fatalf("por seguridad el bridge solo escucha en loopback (%s no lo es)", *addr)
	}
	srv := &http.Server{Addr: *addr, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	log.Printf("bridge escuchando en http://%s (sesión en %s)", *addr, *datos)
	log.Fatal(srv.ListenAndServe())
}
