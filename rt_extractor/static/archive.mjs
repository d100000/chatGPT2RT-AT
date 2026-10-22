// Adapted from CPA2sub2API (MIT), Copyright (c) 2026 Hanhaofu.
// See THIRD_PARTY_NOTICES.md for upstream attribution and license.
const textEncoder = new TextEncoder();

function crc32Table() {
  const table = new Uint32Array(256);

  for (let index = 0; index < 256; index += 1) {
    let value = index;

    for (let bit = 0; bit < 8; bit += 1) {
      value = (value & 1) ? (0xedb88320 ^ (value >>> 1)) : (value >>> 1);
    }

    table[index] = value >>> 0;
  }

  return table;
}

const CRC32_TABLE = crc32Table();

function computeCRC32(bytes) {
  let crc = 0xffffffff;

  for (const byte of bytes) {
    crc = CRC32_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  }

  return (crc ^ 0xffffffff) >>> 0;
}

function normalizeFileName(fileName) {
  // These exports are single documents. Keep only a safe basename so no archive
  // member can write outside its extraction folder on Windows, macOS, or Linux.
  const normalized = String(fileName || "file.json")
    .replaceAll("\\", "/")
    .split("/").filter(Boolean).at(-1) || "file.json";
  const safeName = normalized
    .replace(/[\x00-\x1f\x7f:*?"<>|]/g, "-")
    .replace(/^[. ]+|[. ]+$/g, "");
  if (!safeName) return "file.json";
  return /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(safeName)
    ? `file-${safeName}` : safeName;
}

export function makeUniqueFileNames(fileNames) {
  const used = new Set();
  return fileNames.map((rawName) => {
    const name = normalizeFileName(rawName);
    const dotIndex = name.lastIndexOf(".");
    const base = dotIndex > 0 ? name.slice(0, dotIndex) : name;
    const extension = dotIndex > 0 ? name.slice(dotIndex) : "";
    let candidate = name;
    let suffix = 2;
    // Compare case-insensitively for filesystems that do so, and account for
    // suffixes already present in source filenames rather than overwriting.
    while (used.has(candidate.normalize("NFC").toLowerCase())) {
      candidate = `${base}-${suffix++}${extension}`;
    }
    used.add(candidate.normalize("NFC").toLowerCase());
    return candidate;
  });
}

function getUniqueEntries(entries) {
  const names = makeUniqueFileNames(entries.map((entry, index) => entry.fileName || `file-${index + 1}.json`));
  return entries.map((entry, index) => ({
    fileName: names[index],
    bytes: textEncoder.encode(String(entry.text ?? "")),
  }));
}

function getDosDateTimeParts(date) {
  const safeDate = date instanceof Date && !Number.isNaN(date.getTime()) ? date : new Date();
  const year = Math.min(Math.max(safeDate.getFullYear(), 1980), 2107);
  const month = safeDate.getMonth() + 1;
  const day = safeDate.getDate();
  const hours = safeDate.getHours();
  const minutes = safeDate.getMinutes();
  const seconds = Math.floor(safeDate.getSeconds() / 2);

  return {
    time: (hours << 11) | (minutes << 5) | seconds,
    date: ((year - 1980) << 9) | (month << 5) | day,
  };
}

function writeUint16(view, offset, value) {
  view.setUint16(offset, value & 0xffff, true);
}

function writeUint32(view, offset, value) {
  view.setUint32(offset, value >>> 0, true);
}

export function buildZipArchive(entries, options = {}) {
  if (!Array.isArray(entries) || entries.length > 65535) {
    throw new RangeError("ZIP 最多支持 65535 个文件");
  }
  const normalizedEntries = getUniqueEntries(entries);
  const modifiedAt = options.modifiedAt instanceof Date ? options.modifiedAt : new Date();
  const dosDateTime = getDosDateTimeParts(modifiedAt);
  const localFiles = [];
  const centralDirectory = [];
  let offset = 0;
  let centralDirectorySize = 0;

  normalizedEntries.forEach((entry) => {
    const fileNameBytes = textEncoder.encode(entry.fileName);
    const fileData = entry.bytes;
    if (fileNameBytes.length > 65535 || fileData.length > 0xffffffff) {
      throw new RangeError("文件名或文件内容超过 ZIP 大小限制");
    }
    const crc32 = computeCRC32(fileData);
    const localHeader = new Uint8Array(30 + fileNameBytes.length);
    const localView = new DataView(localHeader.buffer);

    writeUint32(localView, 0, 0x04034b50);
    writeUint16(localView, 4, 20);
    writeUint16(localView, 6, 0x0800);
    writeUint16(localView, 8, 0);
    writeUint16(localView, 10, dosDateTime.time);
    writeUint16(localView, 12, dosDateTime.date);
    writeUint32(localView, 14, crc32);
    writeUint32(localView, 18, fileData.length);
    writeUint32(localView, 22, fileData.length);
    writeUint16(localView, 26, fileNameBytes.length);
    writeUint16(localView, 28, 0);
    localHeader.set(fileNameBytes, 30);

    localFiles.push(localHeader, fileData);

    const centralHeader = new Uint8Array(46 + fileNameBytes.length);
    const centralView = new DataView(centralHeader.buffer);

    writeUint32(centralView, 0, 0x02014b50);
    writeUint16(centralView, 4, 20);
    writeUint16(centralView, 6, 20);
    writeUint16(centralView, 8, 0x0800);
    writeUint16(centralView, 10, 0);
    writeUint16(centralView, 12, dosDateTime.time);
    writeUint16(centralView, 14, dosDateTime.date);
    writeUint32(centralView, 16, crc32);
    writeUint32(centralView, 20, fileData.length);
    writeUint32(centralView, 24, fileData.length);
    writeUint16(centralView, 28, fileNameBytes.length);
    writeUint16(centralView, 30, 0);
    writeUint16(centralView, 32, 0);
    writeUint16(centralView, 34, 0);
    writeUint16(centralView, 36, 0);
    writeUint32(centralView, 38, 0);
    writeUint32(centralView, 42, offset);
    centralHeader.set(fileNameBytes, 46);

    centralDirectory.push(centralHeader);
    centralDirectorySize += centralHeader.length;
    offset += localHeader.length + fileData.length;
  });

  if (offset + centralDirectorySize > 0xffffffff) {
    throw new RangeError("导出内容超过 ZIP 大小限制");
  }
  const endOfCentralDirectory = new Uint8Array(22);
  const endView = new DataView(endOfCentralDirectory.buffer);

  writeUint32(endView, 0, 0x06054b50);
  writeUint16(endView, 4, 0);
  writeUint16(endView, 6, 0);
  writeUint16(endView, 8, normalizedEntries.length);
  writeUint16(endView, 10, normalizedEntries.length);
  writeUint32(endView, 12, centralDirectorySize);
  writeUint32(endView, 16, offset);
  writeUint16(endView, 20, 0);

  return new Blob([...localFiles, ...centralDirectory, endOfCentralDirectory], {
    type: "application/zip",
  });
}
