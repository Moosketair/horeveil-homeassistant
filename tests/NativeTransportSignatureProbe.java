package fr.moosketair.horeveil.qualification;

import com.authlete.hms.ComponentIdentifier;
import com.authlete.hms.SignatureBaseBuilder;
import com.authlete.hms.SignatureField;
import com.authlete.hms.SignatureInputField;
import com.authlete.hms.SignatureMetadata;
import com.authlete.hms.SignatureMetadataParameters;
import com.nimbusds.jose.crypto.impl.ECDSA;
import java.nio.charset.StandardCharsets;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.PublicKey;
import java.security.Signature;
import java.security.SignatureException;
import java.security.spec.ECGenParameterSpec;
import java.time.Instant;
import java.util.Arrays;
import java.util.Base64;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

public final class NativeTransportSignatureProbe {
    private static final String SERVER_ID = "95037ac3-660a-451e-ae17-d9a3e61eb1af";

    static Map<String, String> proof(PrivateKey privateKey, PublicKey publicKey, Instant now) throws Exception {
        String target = "https://qualification.example.invalid/api/horeveil/direct/v1/enroll";
        byte[] body = "{\"probe\":\"qualification\"}".getBytes(StandardCharsets.UTF_8);
        String digest = "sha-256=:" + encode(MessageDigest.getInstance("SHA-256").digest(body)) + ":";
        SignatureMetadata metadata = new SignatureMetadata(Arrays.asList(
                new ComponentIdentifier("@method"), new ComponentIdentifier("@target-uri"),
                new ComponentIdentifier("content-digest"), new ComponentIdentifier("content-type"),
                new ComponentIdentifier("horeveil-server-id")),
                new SignatureMetadataParameters().setAlg("ecdsa-p256-sha256").setCreated(now)
                        .setExpires(now.plusSeconds(120)).setNonce(UUID.randomUUID().toString())
                        .setKeyid("probe").setTag("horeveil-enroll-v1"));
        byte[] signature = new SignatureBaseBuilder((ignored, component) -> {
            switch (component.getComponentName()) {
                case "@method": return "POST";
                case "@target-uri": return target;
                case "content-digest": return digest;
                case "content-type": return "application/json";
                case "horeveil-server-id": return SERVER_ID;
                default: throw new IllegalArgumentException();
            }
        }).build(metadata).sign(base -> {
            try {
                Signature signer = Signature.getInstance("SHA256withECDSA");
                signer.initSign(privateKey);
                signer.update(base);
                return ECDSA.transcodeSignatureToConcat(signer.sign(), 64);
            } catch (Exception error) {
                throw new SignatureException(error);
            }
        });
        Map<String, String> fields = new LinkedHashMap<>();
        fields.put("url", target);
        fields.put("spki", encode(publicKey.getEncoded()));
        fields.put("digest", digest);
        fields.put("server", SERVER_ID);
        fields.put("input", new SignatureInputField(Collections.singletonMap("sig1", metadata)).serialize());
        fields.put("signature", new SignatureField(Collections.singletonMap("sig1", signature)).serialize());
        return fields;
    }

    private static String encode(byte[] bytes) {
        return Base64.getEncoder().encodeToString(bytes);
    }

    public static void main(String[] args) throws Exception {
        KeyPairGenerator generator = KeyPairGenerator.getInstance("EC");
        generator.initialize(new ECGenParameterSpec("secp256r1"));
        KeyPair key = generator.generateKeyPair();
        long offset = args.length == 0 ? 0 : Long.parseLong(args[0]);
        proof(key.getPrivate(), key.getPublic(), Instant.now().plusSeconds(offset)).forEach(
                (name, value) -> System.out.println("INSTRUMENTATION_RESULT: probe_" + name + "=" + value));
    }
}
