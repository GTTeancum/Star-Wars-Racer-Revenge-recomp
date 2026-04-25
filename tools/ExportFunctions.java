// Export function list as CSV for PS2Recomp
// @category PS2Recomp

import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import java.io.File;
import java.io.PrintWriter;

public class ExportFunctions extends GhidraScript {
    @Override
    public void run() throws Exception {
        File outputFile = askFile("Save CSV", "Save");
        PrintWriter writer = new PrintWriter(outputFile);
        writer.println("name,start,end,size");

        FunctionIterator iter = currentProgram.getFunctionManager().getFunctions(true);
        int count = 0;
        while (iter.hasNext()) {
            Function func = iter.next();
            long start = func.getEntryPoint().getOffset();
            long end = func.getBody().getMaxAddress().getOffset() + 1;
            long size = end - start;
            String name = func.getName();
            writer.printf("%s,0x%x,0x%x,%d%n", name, start, end, size);
            count++;
        }

        writer.close();
        println("Exported " + count + " functions to " + outputFile.getAbsolutePath());
    }
}
