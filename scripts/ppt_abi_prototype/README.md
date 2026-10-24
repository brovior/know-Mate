# PowerPoint event ABI prototype

Review-only proof, not linked into the product or installer. It never activates Office,
connects to a live application, registers COM, or calls Office shutdown. Existing product
capability checks, lifecycle interfaces and security policy are unchanged.

From the repository root, using installed .NET Framework and PowerPoint PIA metadata:

```powershell
& .\.venv\Scripts\python.exe .\scripts\ppt_abi_prototype\run.py --output .\.aegisdesk_build\ppt-abi-prototype\review-run
```

The generator reads the PIA with reflection-only loading. It implements all 33 methods
in a synthetic sink. Both `/link` embedding and `/reference` builds run as x86 and x64.
The caller obtains actual CCW interface pointers and invokes native vtable addresses
using `DynamicMethod.EmitCalli` with unmanaged stdcall. It is a CLR-hosted unmanaged ABI
caller, not a standalone C++ program.

`host_contract.json` is independent caller input, captured from the live host metadata
on 2026-10-17 and resolved against its registered typelib without activation. It is not
generated from the sink. The generator uses PIA metadata only for the implementation.
The JSON fixes all 33 names, slots, DISPIDs and native argument types. Runtime production
host validation is not implemented by this prototype.

`NativeMock.cs` constructs native COM object/vtable memory, rather than exposing a managed
CCW as the argument. Callbacks service QI/AddRef/Release only; no Office method is called.
The caller first obtains each declared interface with QI. The observer verifies RCW
status and native identity. Additional typed-interface QI by the CLR is counted
separately: it is not required for an already correctly typed vtable pointer. Every
argument object's AddRef/Release balance must return to its initial owner reference.

Covered: full 33-event vtable and dispatch routing; object identity, enum and float
arguments; Active false/true and initial cancel false/true; 2-byte VARIANT_BOOL canaries;
unrelated cancellation preservation; one injected non-close-event exception HRESULT;
unknown IID; invalid DISPID, argument count and cancellation VARIANT; GC during callbacks;
external COM reference retaining the sink and final release allowing collection.

Historical run5 remains **FAIL under its original exact-VARIANT-tag criterion**, with
logs, binaries and checksums retained. Review identified an acceptance-criterion
mismatch: Automation allows argument coercion. This does not justify a custom dispatch
adapter. See [IDispatch::Invoke](https://learn.microsoft.com/en-us/windows/win32/api/oaidl/nf-oaidl-idispatch-invoke).

New tests characterize VT_I4 inputs 0/123/-1 with Active false/true: converted handler
input, entire original VARIANT array, surrounding canaries, absence of scalar writeback,
and preservation of a separate incoming-true bool buffer. Valid BYREF|BOOL remains the
required cancellation path. A nonconvertible BSTR is rejected before handler entry,
with intact caller buffers. Error HRESULT details are recorded rather than requiring
one CLR-specific error code. These are additional executed tests, not renamed run5 results.

Current run15: **x86/x64 reference candidates PASS; embedded candidates PARTIAL**.
Each runs 282 normal/fault-injected ABI calls. Embedded type information queries still
return TYPE_E_ELEMENTNOTFOUND; their GetIDsOfNames/Invoke work. No embedded candidate
is selected for integration. Direct-reference executions resolve exactly the pinned
PowerPoint and office PIA15 identities from the existing GAC, with runtime paths/hashes
matching build inputs. No PIA files are installed or redistributed. Availability on
another PC and redistribution licensing are not established by this local result.

Additional isolated gates:

- `ContractValidator.cs` is a candidate ITypeInfo parser, not a product integration.
  `ContractFixtures.cs` clones actual registered typelib descriptors in memory and
  rejects 28 mutations before simulated connection: IID/base/size/order/offset,
  function kinds/callconv/HRESULT/arity, FIN/FOUT/retval, pointer/type mismatch,
  reference identity, enum size/identity, missing default interface and alias cycle.
  Reading a VT_USERDEFINED reference uses the 32-bit HREFTYPE union member, not a
  checked conversion of a pointer-sized value. Both architectures pass.
- The generated close handlers assign cancellation before the injectable test hook,
  catch its failure and latch Broken without throwing across COM. Sixteen injected
  close-failure calls per build preserve valid cancellation and inactive behavior.
  Production wiring must check the failure latch before allowing Quit.
- `GuardFixtures.cs` compiles unchanged product guard/events against a managed fake
  host/connection point: 20 cases per architecture cover counts, alerts, reentry,
  disconnect, partial Advise failure, fatal/unknown Quit, Unadvise failure, inactive
  retained sink, and wrong thread/apartment. This is not the combined PIA+guard proof.
- `RollbackFixtures.cs` compiles unchanged product Mutation against explicitly named
  in-memory registry/filesystem/assembly dependencies. Eighteen update write failures
  restore prior registry/record, normal remove+old reinstall works, and same-version
  overwrite remains refused. Environment preflight/version status is modeled; no real
  HKLM, ACL, lease, filesystem durability or UAC conclusion follows from these tests.

Installation remains blocked: the installed 1.0.4.0 reader hash differs from the
current candidate's same-version DLL. No exact complete old payload/setup/manifest
package was found in inspected project build/dist and Temp paths. The proposed
rollback package preservation stopped before copying. Do not repackage a manifest or
override same-version checks to manufacture rollback evidence.

Still pending: independent candidate review; combined PIA sink/parser/product guard
wiring (including Broken admission); safe behavior when PIA is unavailable; a genuine
rollback package or reviewed alternative recovery; cross-apartment transport; actual
Office/update/UAC. Product interfaces, version, build and installer remain untouched.
No diagnostic IID was added. The runner returns nonzero if any comparison is PARTIAL
or FAIL; it preserves each result separately. Timeouts affect only its test children.


## Product integration follow-up (1.0.5.10 candidate)

The earlier run15/loader1 descriptions above are historical evidence. Product sources
now integrate the full contract into a PIA-free core plus optional Events assembly.
`run_product.py --payload <build-info>/ppt_reader --output <new-directory>` tests the
actual two binaries, unlike the earlier prototype comparison. Both architectures use
an independent host-signature JSON, raw unmanaged calli/IDispatch, native-layout mock
arguments, and actual validator/loader/guard together. The synthetic loader entry
bypasses only the protected Program Files boundary; it still validates the installation
record, exact file allowlist, identity, versions, hashes and installed GAC PIA identities.
This does not prove real installation ACLs, Office connection points or apartment transport.

Combined tests cover all33 members, active/initial cancellation states, buffer canaries,
argument reference balance, 28 invalid host contracts, guard reentry/fatal transport,
and inactive retained sinks. Separate processes cover absent/corrupt/wrong-identity
Events, incomplete/foreign/wrong-schema installation records, class/bitness mismatch,
PIA unavailability, and a separately compiled fault-injected sink. The shipping sink's
partial fault checkpoint is erased by the compiler; no runtime observer hook is shipped.
Empty synthetic extraction is checked before/after these failures without persisting text.

`run.py` retains the original embedded/reference comparison and now tests current
Mutation via memory dependencies; combined guard coverage moved to `run_product.py`.
`GuardFixtures.cs` is retained as the earlier two-method-sink fixture, not the current runner.
The installed1.0.4.0 root has since been copied byte-for-byte into a diagnostic snapshot;
that snapshot is not an original complete installation package. Installation stays blocked
pending review and a validated forward recovery package. See `docs/design/PPT_EVENT_ADAPTER.md`.
# Review-fix fixtures

`ProductSetupMemory.cs` + `ProductSetupFixtures.cs` compile the actual
`PptSetupMachine`, `PptSetupMutation`, and `PptSetupOwnership`. Registry, filesystem,
ACL, environment, and payload-validity inputs are fake; Check/Apply/Recover are not.
The runner includes 13 no-write denial cases, repair/upgrade, 18 registry/record
rollback faults and four copy/ACL faults with retry (37 cases per architecture).

`run_product.py` also checks broken callbacks inside Quit and disk/loaded MVID
mismatches for preloaded alternate core/Events builds. The MVID mismatch fixtures
invoke the actual comparison through the guard's factory seam; they do not claim
full LoadFrom cache unification or replacement of a mapped DLL. Positive loader
coverage uses actual product DLLs. No Office process is started.
