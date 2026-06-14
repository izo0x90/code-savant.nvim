# Google Web Search Tool Implementation Design

This document details how the original JS/TS CLI's web search tool works and specifies the high-fidelity Python implementation for our agent engine (`engine/`).

---

## 1. How the Original JS/TS CLI Web Search Works

The original JS/TS CLI does not manually scrape search engines or query secondary Search APIs. Instead, it relies on the native **Google Search Grounding** capability of the Gemini API.

### 1.1 Native Grounding Tool Definition
When active, the CLI declares the search tool to Gemini via the standard tool format:
```json
{
  "tools": [
    {
      "googleSearch": {}
    }
  ]
}
```

### 1.2 The Byte-Based Citation Challenge
When Gemini responds using search grounding, the response includes grounding metadata in `candidates[0].groundingMetadata`. This contains:
* **`groundingChunks`**: The list of search sources (titles, URLs).
* **`groundingSupports`**: Specific segment spans in the text that are backed by the chunks, specified using `startIndex` and `endIndex`.

> [!WARNING]
> **Critical Edge Case:** The `startIndex` and `endIndex` in `groundingSupports` are **byte-based offsets** of the UTF-8 encoded response string, NOT character-based offsets. 
> 
> If you perform standard string slicing in JavaScript (or Python) using these offsets directly on a normal Unicode/UTF-8 string containing multi-byte characters (such as emojis, accented characters, or non-Latin alphabets), you will cut multi-byte characters in half. This results in:
> 1. Invalid/broken Unicode characters in the output.
> 2. Incorrect citation placements, as characters like emoji are 1-2 code points but 4 bytes in UTF-8.

### 1.3 Original JS Splicing Solution
To resolve this, the original TS source code converts the response string into a raw byte array, splices the markdown citation tags (e.g. `[^1]`) at the correct byte offsets, and then decodes the modified byte array back into a UTF-8 string:

```typescript
// Conceptual flow from packages/core/src/tools/web-search.ts
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8");

// Convert text to byte array
let bytes = encoder.encode(responseText);

// Splicing citations based on byte-based offsets (groundingSupports)
// from the end of the text to the beginning to preserve indices:
for (const support of groundingSupports.reverse()) {
  const citationMarker = encoder.encode(`[^${support.chunkIndex + 1}]`);
  
  // Splice byte arrays precisely at the byte-based index
  bytes = concatByteArrays(
    bytes.slice(0, support.endIndex),
    citationMarker,
    bytes.slice(support.endIndex)
  );
}

const finalMarkdown = decoder.decode(bytes);
```

---

## 2. Python Implementation & Improvements

We will implement this as a native, premium-looking tool (`GoogleWebSearchTool`) in Python inside `engine/tools.py`.

### 2.1 Replicating the Byte-Based Splicing in Python
In Python, we must replicate the exact byte-level logic to avoid corrupting multi-byte characters. 

```python
def splice_citations(text: str, grounding_supports: list) -> str:
    """
    Splices citation markers ([^1]) into text using UTF-8 byte offsets
    to prevent splitting multi-byte characters (emojis, Unicode).
    """
    # Convert string to UTF-8 bytes
    text_bytes = bytearray(text.encode("utf-8"))
    
    # Sort supports in descending order by endIndex to avoid offset shifting
    sorted_supports = sorted(
        grounding_supports, 
        key=lambda s: s.get("endIndex", 0), 
        reverse=True
    )
    
    for support in sorted_supports:
        end_idx = support.get("endIndex")
        if end_idx is None or end_idx > len(text_bytes):
            continue
            
        chunk_indices = support.get("groundingChunkIndices", [])
        if not chunk_indices:
            continue
            
        # Format citation suffix (e.g., [^1][^2])
        citation_str = "".join(f"[^{idx + 1}]" for idx in chunk_indices)
        citation_bytes = citation_str.encode("utf-8")
        
        # Splice the citation bytes into the byte array
        text_bytes[end_idx:end_idx] = citation_bytes
        
    return text_bytes.decode("utf-8")
```

### 2.2 Formatting the Grounding Sources as a Premium Table
Along with inline footnotes, we will format the list of search sources (`groundingChunks`) as a clean, highly visual Markdown table appended to the end of the response:

```python
def format_grounding_sources(chunks: list) -> str:
    """Formats grounding chunks into a premium, clean Markdown source table."""
    if not chunks:
        return ""
        
    lines = [
        "\n\n### 🔍 Search Sources",
        "| Source | Title | Link |",
        "| :---: | :--- | :--- |"
    ]
    
    for idx, chunk in enumerate(chunks):
        web = chunk.get("web", {})
        title = web.get("title", "Untitled Source")
        url = web.get("uri", "#")
        
        # Shorten and clean URL for presentation
        display_url = url.replace("https://", "").replace("www.", "").split("/")[0]
        
        lines.append(f"| [^{idx + 1}] | {title} | [{display_url}]({url}) |")
        
    return "\n".join(lines)
```

### 2.3 Registration in `engine/`
1. **Tool Definition:** Added to `engine/tools.py` mapping to native `types.GoogleSearch()`.
2. **Registry Integration:** Managed dynamically inside `engine/registry.py` under the `ToolRegistry` so that sessions can toggle search grounding on/off based on the active agent system prompt or parameters.
