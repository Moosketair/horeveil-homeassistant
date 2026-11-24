package fr.moosketair.horeveil.qualification;

import android.app.Activity;
import android.app.Instrumentation;
import android.os.Bundle;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.spec.ECGenParameterSpec;
import java.time.Instant;
import java.util.Map;

public final class NativeTransportKeyProbe extends Instrumentation {
    @Override public void onCreate(Bundle arguments) {
        super.onCreate(arguments);
        start();
    }

    @Override public void onStart() {
        Bundle result = new Bundle();
        try {
            String alias = "horeveil-qualification";
            KeyStore store = KeyStore.getInstance("AndroidKeyStore");
            store.load(null);
            if (!store.containsAlias(alias)) {
                KeyPairGenerator generator = KeyPairGenerator.getInstance("EC", "AndroidKeyStore");
                generator.initialize(new KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN)
                        .setAlgorithmParameterSpec(new ECGenParameterSpec("secp256r1"))
                        .setDigests(KeyProperties.DIGEST_SHA256).setUserAuthenticationRequired(false).build());
                generator.generateKeyPair();
            }
            KeyStore.PrivateKeyEntry key = (KeyStore.PrivateKeyEntry) store.getEntry(alias, null);
            if (key.getPrivateKey().getEncoded() != null) throw new IllegalStateException("Key is exportable");
            Map<String, String> proof = NativeTransportSignatureProbe.proof(
                    key.getPrivateKey(), key.getCertificate().getPublicKey(), Instant.now());
            proof.forEach((name, value) -> result.putString("probe_" + name, value));
            finish(Activity.RESULT_OK, result);
        } catch (Exception error) {
            result.putString("probe_failure", error.toString());
            finish(Activity.RESULT_CANCELED, result);
        }
    }
}
