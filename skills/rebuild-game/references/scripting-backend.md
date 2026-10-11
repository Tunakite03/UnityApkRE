# Scripting Backend Reference

Unity Android games use one of two scripting backends. The backend determines what can be
recovered and how scripts are rebuilt.

## Mono backend

**Detection**: `report.json` → `scripting_backend: "mono"`, and `.dll` files exist in
`extracted/Data/Managed/`.

**What you get**: full C# source code via decompilation. ILSpy or dnSpy can recover
nearly all logic, control flow, variable names, and type information.

### Rebuild strategy

1. Decompile game assemblies (filter out system/Unity DLLs):
   ```bash
   uare mono-decompile <workdir>/extracted/Data/Managed --out <output>/Assets/Scripts
   ```

2. The decompiler creates one `.cs` file per type, organized by namespace.

3. **Common issues after decompilation:**
   - Compiler-generated names (`<>c__DisplayClass0_0`, `<Start>d__5`) -- these are
     lambdas and coroutines, leave them as-is
   - Missing `using` directives -- add them based on compile errors
   - Property backing fields shown explicitly -- the decompiler handles this
   - Unsafe code blocks -- may need `AllowUnsafeBlocks` in .asmdef

4. **For "Local only" scope**: after decompilation, find and stub:
   - `WWW`, `UnityWebRequest` calls → return empty/default
   - SDK-specific classes (search for PlayFab, Firebase, Photon namespaces)
   - License/auth checks → return success

### Assemblies to skip

These are system/engine assemblies that Unity provides:

- `mscorlib`, `System`, `System.*`, `Microsoft.*`, `Mono.*`
- `UnityEngine`, `UnityEngine.*`, `Unity.*`
- `Newtonsoft.Json` (usually bundled but Unity has its own)
- `netstandard`

The `uare mono-decompile` command filters these automatically.

---

## IL2CPP backend

**Detection**: `report.json` → `scripting_backend: "il2cpp"`, and `libil2cpp.so` +
`global-metadata.dat` exist.

**What you get**: type names, method names, field names, property names, event names.
**What you don't get**: method bodies, local variables, control flow.

### Recovery levels

| Level | What | Tools needed | Command |
|---|---|---|---|
| Names only | type/method/field/property/event names grouped by declaring type | None | `uare dump-il2cpp` |
| Names + RVAs | above + method addresses in the binary | Il2CppDumper | `uare il2cpp-enrich` |
| Names + pseudo-C | above + Ghidra decompiled C for each method | Ghidra + Il2CppDumper | `uare decompile` + `uare il2cpp-annotate` |

### Rebuild strategy

**Level 1: Names only (always available)**

```bash
uare dump-il2cpp <workdir>/extracted --out <workdir>/il2cpp
uare il2cpp-annotate <workdir>/il2cpp --out <output>/Assets/Scripts/IL2CPP
```

This produces `.cs` stub files with:
```csharp
namespace Game {
    public class Player {
        public object health;
        public object maxHealth;

        public void Update() {
            throw new System.NotImplementedException();
        }

        public void TakeDamage() {
            throw new System.NotImplementedException();
        }
    }
}
```

**Level 2: With RVAs (needs Il2CppDumper)**

Add `--enrich` to get method addresses:
```csharp
// RVA: 0x1A2B3C
public void TakeDamage() { ... }
```

**Level 3: With pseudo-C (needs Ghidra)**

Add `--ghidra-dir` to get decompiled method bodies as comments:
```csharp
// RVA: 0x1A2B3C
// Ghidra pseudo-C:
// int x = *(this + 0x18);
// x = x - param1;
// if (x < 0) x = 0;
// *(this + 0x18) = x;
public void TakeDamage() { ... }
```

### For "Local only" scope with IL2CPP

1. Generate stub classes from names
2. Make all methods return default values or throw `NotImplementedException`
3. Ensure the project compiles (add `using` directives, fix type references)
4. The stubs serve as documentation of the game's structure

### For "Backend stubs" scope with IL2CPP

1. Generate stub classes
2. Identify service-related types (names containing `Service`, `Manager`, `API`, `Server`, `Client`, `Network`)
3. Create interface abstractions for those types
4. Implement with Debug.Log stubs

---

## Detecting third-party SDKs

Search IL2CPP names or Mono assemblies for these patterns:

| Pattern | SDK |
|---|---|
| `PlayFab*` | PlayFab (Azure) |
| `Firebase*` | Firebase (Google) |
| `Photon*`, `Pun*` | Photon (networking/multiplayer) |
| `Mirror*` | Mirror (open-source networking) |
| `Nakama*` | Nakama (open-source backend) |
| `GameSparks*` | GameSparks |
| `AppLovin*`, `MaxSdk*` | AppLovin MAX (ads) |
| `IronSource*` | ironSource (ads) |
| `AdMob*`, `GoogleMobileAds*` | Google AdMob |
| `Facebook*`, `Meta*` | Meta SDK |
| `Amplitude*` | Amplitude (analytics) |
| `Adjust*` | Adjust (attribution) |
| `Singular*` | Singular (attribution) |

Report detected SDKs to the user during the checkpoint so they can decide what to stub.
