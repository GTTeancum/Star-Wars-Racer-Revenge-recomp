// Find the game's GS initialization function chain
// @category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;

public class FindGSInit extends GhidraScript {
    @Override
    public void run() throws Exception {
        // Functions that write to GS registers (0x12000000+ mapped area)
        // or do DMA to GIF channel. From our binary search:
        // 0x258E70 has concentrated GS register writes.
        
        println("=== GS init function chain ===");
        Function gsInit = getFunctionAt(toAddr(0x258E70));
        if (gsInit != null) {
            println("GS init: " + gsInit.getName() + " @ " + gsInit.getEntryPoint());
            // Callers
            for (Function caller : gsInit.getCallingFunctions(null)) {
                println("  <- " + caller.getName() + " @ " + caller.getEntryPoint());
                for (Function gc : caller.getCallingFunctions(null)) {
                    println("    <- " + gc.getName() + " @ " + gc.getEntryPoint());
                    for (Function ggc : gc.getCallingFunctions(null)) {
                        println("      <- " + ggc.getName() + " @ " + ggc.getEntryPoint());
                    }
                }
            }
        }
        
        // Also check 0x258C70 and nearby functions
        println("\n=== GS-related functions (0x258000-0x25A000) ===");
        FunctionIterator iter = currentProgram.getListing().getFunctions(toAddr(0x258000), true);
        while (iter.hasNext()) {
            Function fn = iter.next();
            if (fn.getEntryPoint().getOffset() > 0x25A000) break;
            int callerCount = 0;
            for (Function c : fn.getCallingFunctions(null)) callerCount++;
            println("  " + fn.getName() + " @ " + fn.getEntryPoint() + " callers=" + callerCount);
        }
        
        // Check what calls the DMA GIF submit at 0x243690
        println("\n=== DMA GIF submit callers (0x243690) ===");
        Function dmaGif = getFunctionAt(toAddr(0x243690));
        if (dmaGif == null) dmaGif = getFunctionContaining(toAddr(0x243690));
        if (dmaGif != null) {
            println("DMA GIF: " + dmaGif.getName() + " @ " + dmaGif.getEntryPoint());
            for (Function caller : dmaGif.getCallingFunctions(null)) {
                println("  <- " + caller.getName() + " @ " + caller.getEntryPoint());
            }
        }
        
        // Find sceGsResetGraph or similar init functions
        println("\n=== Functions calling GsCrt (syscall 0x02) ===");
        // GsSetCrt is typically called during GS init
        Function gsCrt = getFunctionAt(toAddr(0x2F5790)); // Near syscall area
        if (gsCrt != null) {
            for (Function caller : gsCrt.getCallingFunctions(null)) {
                println("  " + caller.getName() + " @ " + caller.getEntryPoint());
            }
        }
        
        println("\n=== Done ===");
    }
}
