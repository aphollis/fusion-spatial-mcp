import net from "node:net";

interface Pending {
  resolve: (v: unknown) => void;
  reject: (e: Error) => void;
  timer: NodeJS.Timeout;
}

/**
 * Persistent TCP connection to the FusionSpatialMCP add-in running inside
 * Fusion 360. Speaks newline-delimited JSON: {id, method, params} ->
 * {id, result} or {id, error: {message, traceback}}.
 */
export class FusionBridge {
  private socket: net.Socket | null = null;
  private connecting: Promise<net.Socket> | null = null;
  private buffer = "";
  private nextId = 1;
  private pending = new Map<number, Pending>();

  constructor(
    private host = "127.0.0.1",
    private port = 8767,
  ) {}

  private connect(): Promise<net.Socket> {
    if (this.socket && !this.socket.destroyed) {
      return Promise.resolve(this.socket);
    }
    if (this.connecting) return this.connecting;

    this.connecting = new Promise<net.Socket>((resolve, reject) => {
      const sock = net.createConnection({ host: this.host, port: this.port });
      sock.setNoDelay(true);

      const onConnectError = (err: Error) => {
        this.connecting = null;
        reject(
          new Error(
            `Could not reach the Fusion listener on ${this.host}:${this.port} (${err.message}). ` +
              `Make sure Fusion 360 is open and the FusionSpatialMCP add-in is running: ` +
              `press Shift+S (Utilities > Add-Ins), select FusionSpatialMCP on the Add-Ins tab, ` +
              `and click Run. The listener keeps running until Fusion closes or the add-in is stopped.`,
          ),
        );
      };
      sock.once("error", onConnectError);

      sock.once("connect", () => {
        sock.removeListener("error", onConnectError);
        this.socket = sock;
        this.connecting = null;

        sock.on("data", (chunk) => this.onData(chunk));
        sock.on("error", () => {
          /* handled by close */
        });
        sock.on("close", () => {
          this.socket = null;
          this.failAll(new Error("Connection to Fusion was closed."));
        });
        resolve(sock);
      });
    });
    return this.connecting;
  }

  private onData(chunk: Buffer): void {
    this.buffer += chunk.toString("utf8");
    let idx: number;
    while ((idx = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;
      let msg: any;
      try {
        msg = JSON.parse(line);
      } catch {
        continue;
      }
      const p = this.pending.get(msg.id);
      if (!p) continue;
      this.pending.delete(msg.id);
      clearTimeout(p.timer);
      if (msg.error) {
        const detail = msg.error.traceback
          ? `${msg.error.message}\n${msg.error.traceback}`
          : String(msg.error.message ?? msg.error);
        p.reject(new Error(detail));
      } else {
        p.resolve(msg.result);
      }
    }
  }

  private failAll(err: Error): void {
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(err);
    }
    this.pending.clear();
  }

  async call(method: string, params: Record<string, unknown> = {}, timeoutMs = 90_000): Promise<any> {
    const sock = await this.connect();
    const id = this.nextId++;
    const payload = JSON.stringify({ id, method, params }) + "\n";

    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(
          new Error(
            `Fusion did not answer '${method}' within ${timeoutMs / 1000}s. ` +
              `Fusion may be busy computing, or a modal dialog may be open on screen.`,
          ),
        );
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      sock.write(payload, (err) => {
        if (err) {
          this.pending.delete(id);
          clearTimeout(timer);
          reject(err);
        }
      });
    });
  }
}
