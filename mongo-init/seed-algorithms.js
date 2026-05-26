db = db.getSiblingDB("c2pa");

db.supported_algorithms.createIndex({ alg: 1 }, { name: "alg_unique", unique: true });

db.supported_algorithms.updateOne(
  { alg: "me.deepmark.audio.aware.20" },
  {
    $setOnInsert: {
      alg: "me.deepmark.audio.aware.20",
      type: "watermark",
      bindingBits: 20,
      mediaTypes: [
        "audio/wav",
        "audio/wave",
        "audio/x-wav",
        "audio/mpeg",
        "audio/flac",
        "audio/x-flac",
        "audio/ogg"
      ],
      url: "http://watermark-aware-20:9004"
    }
  },
  { upsert: true }
);
