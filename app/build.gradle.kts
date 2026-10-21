plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.sharedclipboard.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.sharedclipboard.app"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }

    // A fixed signing key. With it, a new build installs over the old app and keeps the pairing.
    signingConfigs {
        getByName("debug") {
            storeFile = file("shared-clipboard.keystore")
            storePassword = "android"
            keyAlias = "key0"
            keyPassword = "android"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
}
