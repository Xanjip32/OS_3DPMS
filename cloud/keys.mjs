import { generateKeyPairSync, randomBytes } from "node:crypto";

const { privateKey } = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
const { x, y, d } = privateKey.export({ format: "jwk" });
const publicKey = Buffer.concat([Buffer.of(4), Buffer.from(x, "base64url"), Buffer.from(y, "base64url")]);
console.log("CONNECTOR_KEY=" + randomBytes(32).toString("base64url"));
console.log("VAPID_PUBLIC_KEY=" + publicKey.toString("base64url"));
console.log("VAPID_PRIVATE_KEY=" + d);
