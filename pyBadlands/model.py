import time
import numpy as np

from scipy.spatial import cKDTree
from pyBadlands import (diffLinear, flowNetwork, buildMesh, waveSed,  #oceanDyn,
                        checkPoints, buildFlux, xmlParser, carbGrowth,
                        pelagicGrowth)

# Profiling support
import cProfile
import os
import pstats
import io

#from pyBadlands.libUtils  import simswan as swan

class Model(object):
    """State object for the pyBadlands model."""

    def __init__(self):
        """
        Constructor.
        """

        # Simulation state
        self.tNow = 0.
        self.waveID = 0
        self.outputStep = 0
        self.disp = None
        self.prop = None
        self.carbval = None
        self.carbval2 = None
        self.carbMaxGrowthSp1 = None
        self.carbMaxGrowthSp2 = None
        self.next_carbStep = None
        self.pelaval = None
        self.applyDisp = False
        self.simStarted = False

    def load_xml(self, filename, verbose=False):
        """
        Load an XML configuration file.
        Parameters
        ----------
        filename : string
            Path to the XML file to load.
        verbose : bool
            When True, output additional debug information.
        """

        np.seterr(divide='ignore',invalid='ignore')

        # Only the first node should create a unique output dir
        self.input = xmlParser.xmlParser(filename, makeUniqueOutputDir=True)
        self.tNow = self.input.tStart

        # Seed the random number generator consistently on all nodes
        seed = None
        # limit to max uint32
        seed = np.random.mtrand.RandomState().tomaxint() % 0xFFFFFFFF
        np.random.seed(seed)

        # If there's no demfile specified, we assume that it will be loaded
        # later using build_mesh
        if self.input.demfile:
             self.build_mesh(self.input.demfile, verbose)

        # Initialise carbonate evolution if any
        if self.input.carbonate:
            self.carb = carbGrowth.carbGrowth(self.input, self.recGrid.regX, self.recGrid.regY,
                                              self.carbTIN.tinBase)
            if self.input.coastdist>0.:
                self.carb.buildReg(self.FVmesh.node_coords[:,:2])

        # Initialise pelagic evolution if any
        if self.input.pelagic:
            self.pelagic = pelagicGrowth.pelagicGrowth(self.input)

        # Initialize TIN mobilized wave sediments going to fluvial flowNetwork
        self.waveMobile = np.zeros(self.totPts, dtype=float)
        self.newWaveED = np.zeros(self.totPts, dtype=float)

    def build_mesh(self, filename, verbose):
        # Construct Badlands mesh and grid to run simulation
        self.recGrid, self.FVmesh, self.force, self.tMesh, self.lGIDs, self.fixIDs, self.inIDs, parentIDs, \
        self.inGIDs, self.totPts, self.elevation, self.cumdiff, self.cumhill, self.cumfail, self.cumflex, self.strata, self.mapero, \
        self.tinFlex, self.flex, self.wave, self.straTIN, self.carbTIN = buildMesh.construct_mesh(self.input, filename, verbose)

        if self.input.waveSed:
            self.wavediff = np.zeros((self.totPts))
        else:
            self.wavediff = None

        # Initialize TIN slope (gradient)
        self.slopeTIN = np.zeros(self.totPts, dtype=float)

        # Define hillslope parameters
        self.rain = np.zeros(self.totPts, dtype=float)
        self.hillslope = diffLinear()
        self.hillslope.CDaerial = self.input.CDa
        self.hillslope.CDmarine = self.input.CDm
        self.hillslope.CDriver = self.input.CDr
        self.hillslope.Sc = self.input.Sc
        self.hillslope.Sfail = self.input.Sfail
        self.hillslope.Cfail = self.input.Cfail
        self.hillslope.Sc = self.input.Sc
        self.hillslope.updatedt = 0

        # Define flow parameters
        self.flow = flowNetwork(self.input)

        if self.input.erolays is None:
            self.flow.erodibility = np.full(self.totPts, self.input.SPLero)
        else:
            self.flow.erodibility = self.mapero.erodibility
        self.flow.mindt = self.input.minDT
        self.flow.xycoords = self.FVmesh.node_coords[:,:2]
        self.flow.spl = self.input.spl
        self.flow.depo = self.input.depo
        self.flow.xgrid = None

        reassignID = np.where(parentIDs < len(parentIDs))[0]
        if(len(reassignID)>0):
            tmpTree = cKDTree(self.flow.xycoords[len(parentIDs):,:2])
            distances, indices = tmpTree.query(self.flow.xycoords[reassignID,:2], k=1)
            indices += len(parentIDs)
            parentIDs[reassignID] = indices

        self.flow.parentIDs = parentIDs

        # Define hydrodynamic conditions
        if self.input.waveOn:
            self.force.next_wave = self.input.tStart
            self.wave.build_tree(self.FVmesh.node_coords[:,:2])
            self.wave.swan_init(self.input, self.elevation, self.waveID, self.force.sealevel)
        else:
            if self.input.waveSed:
                self.force.next_wave = self.input.tStart + self.input.tWave
            else:
                self.force.next_wave = self.input.tEnd + 1.e5

        if self.input.carb:

            self.next_carbStep = self.input.tStart + self.input.tCarb
            self.oldsed = np.zeros(len(self.elevation))
            if self.carbTIN is not None:
                self.prop = np.zeros((self.totPts,self.carbTIN.nbSed))
        else:
            self.next_carbStep = self.input.tEnd + 1.e5
            self.prop = np.zeros((self.totPts,1))

    def rebuild_mesh(self, verbose=False):
        """
        Build TIN after 3D displacements.
        """

        # Build the Finite Volume representation
        self.fixIDs = self.recGrid.boundsPt + self.recGrid.edgesPt
        self.FVmesh, self.tMesh, self.lGIDs, self.inIDs, \
            self.inGIDs, self.totPts = buildMesh.reconstruct_mesh(self.recGrid,
                                                                  self.input, verbose)

        # Update edges elevation
        tree1 = cKDTree(self.FVmesh.node_coords[self.fixIDs:,:2])
        tmpelev = self.elevation[self.fixIDs:]
        distances, indices = tree1.query(self.FVmesh.node_coords[:self.fixIDs,:2], k=1)
        self.elevation[:self.fixIDs] = tmpelev[indices]
        self.hillslope.ids = None

        # Reset TIN kdtree and rain
        self.force.update_force_TIN(self.FVmesh.node_coords[:,:2])
        self.rain = np.zeros(self.totPts, dtype=float)
        self.rain[self.inIDs] = self.force.get_Rain(self.tNow, self.elevation, self.inIDs)

        # Update flexural isostasy
        if self.input.flexure:
            self.tinFlex = np.zeros(self.totPts, dtype=float)
            self.flex.update_flexure_parameters(self.FVmesh.node_coords[:,:2])

        # Update SWAN mesh
        if self.input.waveOn:
            self.wave.build_tree(self.FVmesh.node_coords[:,:2])

        # Update stratigraphic mesh
        if self.input.stratdx > 0:
            self.strata.update_TIN(self.FVmesh.node_coords[:, :2])

        # Update erodibility maps
        if self.input.erolays is None:
            self.flow.erodibility = np.full(self.totPts, self.input.SPLero)
        else:
            self.flow.erodibility = self.mapero.erodibility

        # Update Wavesed grid interpolation
        if self.input.waveSed:
            wave.build_tree(self.FVmesh.node_coords[:,:2])

        # Update Carbonate mesh
        #if self.input.carbonate:
        #    if self.input.coastdist>0.:
        #        self.carb.buildReg(self.FVmesh.node_coords[:,:2])

        self.flow.xycoords = self.FVmesh.node_coords[:, :2]
        self.flow.xgrid = None
        self.flow.sedload = None
        self.flow.flowdensity = None
        self.flow.domain = None
        self.hillslope.updatedt = 0

        self.carbval = None
        self.carbval2 = None
        self.pelaval = None
        self.prop = np.zeros((self.totPts,1))

    def run_to_time(self, tEnd, profile=False, verbose=False):
        """
        Run the simulation to a specified point in time (tEnd).
        Parameters
        ----------
        tEnd : float
            Run the simulation to this many years.
        profile : bool
            If True, dump cProfile output to /tmp.
        verbose : bool
            If True, output additional debug information.
        """
        if profile:
            pid = os.getpid()
            pr = cProfile.Profile()
            pr.enable()

        assert hasattr(self, 'recGrid'), "DEM file has not been loaded. Configure one in your XML file or call the build_mesh function."

        if tEnd > self.input.tEnd:
            print('Specified end time is greater than the one used in the XML input file and has been adjusted!')
            tEnd = self.input.tEnd

        # Define non-flow related processes times
        if not self.simStarted:
            self.force.next_rain = self.force.T_rain[0, 0]
            self.force.next_disp = self.force.T_disp[0, 0]
            self.force.next_carb = self.force.T_carb[0, 0]

            self.force.next_display = self.input.tStart
            if self.input.laytime>0:
                self.force.next_layer = self.input.tStart + self.input.laytime
            else:
                self.force.next_layer = self.input.tEnd + 1000.
            self.exitTime = self.input.tEnd
            if self.input.flexure:
                self.force.next_flexure = self.input.tStart + self.input.ftime
            else:
                self.force.next_flexure = self.exitTime + self.input.tDisplay
            self.simStarted = True

        outStrata = 0
        last_time = time.clock()
        last_output = time.clock()

        # Perform main simulation loop
        while self.tNow < tEnd:
            # At most, display output every 5 seconds
            tloop = time.clock() - last_time
            if time.clock() - last_output >= 5.0:
                print('tNow = %s (step took %0.02f seconds)' % (self.tNow, tloop))
                last_output = time.clock()
            last_time = time.clock()

            # Load precipitation rate
            if self.force.next_rain <= self.tNow and self.force.next_rain < self.input.tEnd:
                if self.tNow == self.input.tStart:
                    ref_elev = buildMesh._get_reference_elevation(self.input, self.recGrid, self.elevation)
                    self.force.getSea(self.tNow,self.input.udw,ref_elev)
                self.rain = np.zeros(self.totPts, dtype=float)
                self.rain[self.inIDs] = self.force.get_Rain(self.tNow, self.elevation, self.inIDs)


            # Initialize waveFlux at tStart
            if self.tNow == self.input.tStart:
                self.force.initWaveFlux(self.inIDs)

            # Load tectonic grid
            if not self.input.disp3d:
                # Vertical displacements
                if self.force.next_disp <= self.tNow and self.force.next_disp < self.input.tEnd:
                    ldisp = np.zeros(self.totPts, dtype=float)
                    ldisp.fill(-1.e6)
                    ldisp[self.inIDs] = self.force.load_Tecto_map(self.tNow,self.inIDs)
                    self.disp = self.force.disp_border(ldisp, self.FVmesh.neighbours,
                                                       self.FVmesh.edge_length, self.recGrid.boundsPt)
                    self.applyDisp = True
            else:
                # 3D displacements
                if self.force.next_disp <= self.tNow and self.force.next_disp < self.input.tEnd:
                    if self.input.laytime == 0:
                        updateMesh = self.force.load_Disp_map(self.tNow, self.FVmesh.node_coords[:, :2], self.inIDs)
                    else:
                        # Define 3D displacements on the stratal regions
                        if self.strata is not None:
                            updateMesh, regdX, regdY = self.force.load_Disp_map(self.tNow, self.FVmesh.node_coords[:, :2],
                                                                  self.inIDs, True, self.strata.xyi, self.strata.ids)
                        else:
                            updateMesh = self.force.load_Disp_map(self.tNow, self.FVmesh.node_coords[:, :2], self.inIDs)

                    # Update mesh when a 3D displacements field has been loaded
                    if updateMesh:
                        self.force.dispZ = self.force.disp_border(self.force.dispZ, self.FVmesh.neighbours,
                                           self.FVmesh.edge_length, self.recGrid.boundsPt)
                        # Define flexural flags
                        fflex = 0
                        flexiso = None
                        if self.input.flexure:
                            flexiso = self.cumflex
                            fflex = 1
                        # Define stratal flags
                        fstrat = 0
                        sload = None
                        if self.input.udw == 1 and self.tNow == self.input.tStart and self.strata is not None:
                            self.strata.oldload = np.zeros(len(self.elevation), dtype=float)
                        if self.strata is not None:
                            if self.strata.oldload is None:
                                self.strata.oldload = np.zeros(len(self.elevation), dtype=float)
                        if self.input.laytime > 0 and self.strata.oldload is not None:
                            sload = self.strata.oldload
                            fstrat = 1
                        # Define erodibility map flags
                        fero = 0
                        vKe = None
                        vTh = None
                        if self.input.erolays >= 0:
                            fero = 1
                            vKe = self.mapero.Ke
                            vTh = self.mapero.thickness
                        # Apply horizontal displacements
                        self.recGrid.tinMesh, self.elevation, self.cumdiff, self.cumhill, self.cumfail, self.wavediff, fcum, scum, Ke, Th = self.force.apply_XY_dispacements(
                            self.recGrid.areaDel, self.fixIDs, self.elevation, self.cumdiff, self.cumhill, self.cumfail, self.wavediff,
                            tflex=flexiso, scum=sload, Te=vTh,Ke=vKe, flexure=fflex, strat=fstrat, ero=fero)
                        # Update relevant parameters in deformed TIN
                        if fflex == 1:
                            self.cumflex = fcum
                        if fero == 1:
                            self.mapero.Ke = Ke
                            self.mapero.thickness = Th
                        # Rebuild the computational mesh
                        self.rebuild_mesh(verbose)

                        # Update the stratigraphic mesh
                        if self.input.laytime > 0 and self.strata is not None:
                            self.strata.move_mesh(regdX, regdY, scum, verbose)

            # Compute isostatic flexure
            if self.tNow >= self.force.next_flexure:
                flextime = time.clock()
                ref_elev = buildMesh._get_reference_elevation(self.input, self.recGrid, self.elevation)
                self.force.getSea(self.tNow,self.input.udw,ref_elev)
                self.tinFlex = self.flex.get_flexure(self.elevation, self.cumdiff,
                            self.force.sealevel,self.recGrid.boundsPt, initFlex=False)
                # Get border values
                self.tinFlex = self.force.disp_border(self.tinFlex, self.FVmesh.neighbours,
                                                      self.FVmesh.edge_length, self.recGrid.boundsPt)
                # Update flexural parameters
                self.elevation += self.tinFlex
                self.cumflex += self.tinFlex
                # Update next flexure time
                self.force.next_flexure += self.input.ftime
                print("   - Compute flexural isostasy %0.02f seconds" % (time.clock() - flextime))

            # Compute wavesed parameters
            if self.tNow >= self.force.next_wave:
                wavetime = time.clock()
                if self.carbTIN is not None:
                    # Update erosion/deposition due to SPM processes on carbTIN
                    self.carbTIN.update_layers(self.cumdiff-self.oldsed,self.elevation)
                    self.carbTIN.get_active_layer(self.input.wEro)
                    actlay = self.carbTIN.alay
                else:
                    actlay = None
                # Compute wave field and associated bottom current conditions
                waveED,nactlay = self.wave.compute_wavesed(self.tNow, self.input, self.force,
                                                   self.elevation, actlay)

                print("Maximum wave dep: ")
                print(max(waveED))

                # Wave-remobilized sediments sent to stream network if mobilized over steep slopes
                slopeVal = 0.01
                slopeBool = (self.slopeTIN > slopeVal).astype(int)
                waveDep = waveED.clip(min=0) # keep positive values (deposition)
                self.waveMobile = np.multiply(slopeBool,waveDep)
                self.newWaveED = np.subtract(waveED, self.waveMobile)
                self.force.waveFlux = np.multiply(self.waveMobile,self.FVmesh.control_volumes)/self.input.tWave
                print("max wave flux: ")
                print(max(self.force.waveFlux))
                print("min wave flux: ")
                print(min(self.force.waveFlux))

                # Update elevation / cumulative changes based on wave-induced sediment transport
                self.elevation += self.newWaveED
                self.cumdiff  += self.newWaveED
                self.wavediff  += self.newWaveED
                print("   - Compute wave-induced sediment transport %0.02f seconds" % (time.clock() - wavetime))
                # Update carbonate active layer
                if nactlay is not None:
                    self.carbTIN.update_active_layer(nactlay,self.elevation)
                # Update next wave time step
                self.force.next_wave += self.input.tWave

            # Compute carbonate evolution
            if self.tNow >= self.next_carbStep:
                carbtime = time.clock()
                depth = self.elevation-self.force.sealevel
                if self.carbTIN is not None:
                    # Update erosion/deposition due to river and diffusion on carbTIN
                    self.carbTIN.update_layers(self.cumdiff-self.oldsed,self.elevation)

                # Compute reef growth
                if self.input.carbonate:

                    # Load carbonate growth rates for species 1 and 2 during a given growth event
                    if self.force.next_carb <= self.tNow and self.force.next_carb < self.input.tEnd:
                        self.carbMaxGrowthSp1, self.carbMaxGrowthSp2 = self.force.get_carbGrowth(self.tNow, self.inIDs)
                    self.carbval, self.carbval2 = self.carb.computeCarbonate(self.force.meanH, self.cumdiff-self.oldsed,
                                                            depth, self.carbMaxGrowthSp1, self.carbMaxGrowthSp2, self.input.tCarb)

                    if self.carbval2 is not None:
                        self.cumdiff +=  self.carbval + self.carbval2
                        self.elevation += self.carbval + self.carbval2
                    else:
                        self.cumdiff +=  self.carbval
                        self.elevation += self.carbval
                    if self.carbTIN is not None:
                        self.carbTIN.paleoDepth[:,self.carbTIN.step] = self.elevation
                        self.carbTIN.depoThick[:,self.carbTIN.step,1] += self.carbval
                        self.carbTIN.layerThick[:,self.carbTIN.step] += self.carbval
                        if self.carbval2 is not None:
                            self.carbTIN.depoThick[:,self.carbTIN.step,2] += self.carbval2
                            self.carbTIN.layerThick[:,self.carbTIN.step] += self.carbval2
                # Compute pelagic rain
                if self.input.pelagic:
                    self.pelaval = self.pelagic.computePelagic(depth, self.input.tCarb)
                    self.cumdiff +=  self.pelaval
                    self.elevation += self.pelaval
                    if self.carbTIN is not None:
                        self.carbTIN.paleoDepth[:,self.carbTIN.step] = self.elevation
                        self.carbTIN.depoThick[:,self.carbTIN.step,0] += self.pelaval
                        self.carbTIN.layerThick[:,self.carbTIN.step] += self.pelaval
                # Update proportion based on top layer
                if self.prop is not None:
                    ids = np.where(self.carbTIN.layerThick[:,self.carbTIN.step]>0.)[0]
                    self.prop.fill(0.)
                    self.prop[ids,0] = self.carbTIN.depoThick[ids,self.carbTIN.step,0]/self.carbTIN.layerThick[ids,self.carbTIN.step]
                    if self.input.carbonate:
                        self.prop[ids,1] = self.carbTIN.depoThick[ids,self.carbTIN.step,1]/self.carbTIN.layerThick[ids,self.carbTIN.step]
                        if self.carbval2 is not None:
                            self.prop[ids,2] = self.carbTIN.depoThick[ids,self.carbTIN.step,2]/self.carbTIN.layerThick[ids,self.carbTIN.step]

                # Update current cumulative erosion deposition
                self.oldsed = np.copy(self.cumdiff)
                self.next_carbStep += self.input.tCarb

                print("   - Compute carbonate growth %0.02f seconds" % (time.clock() - carbtime))

            # Compute stream network
            self.fillH, self.elevation = buildFlux.streamflow(self.input, self.FVmesh, self.recGrid, self.force, self.hillslope, \
                                              self.flow, self.elevation, self.lGIDs, self.rain, self.tNow, verbose)

            # Create checkpoint files and write HDF5 output
            if self.tNow >= self.force.next_display:

                if self.force.next_display > self.input.tStart:
                    outStrata = 1

                checkPoints.write_checkpoints(self.input, self.recGrid, self.lGIDs, self.inIDs, self.tNow,
                                            self.FVmesh, self.tMesh, self.force, self.flow, self.rain,
                                            self.elevation, self.fillH, self.cumdiff, self.cumhill, self.cumfail, self.wavediff,
                                            self.outputStep, self.prop, self.slopeTIN, self.mapero, self.cumflex)

                if self.straTIN is not None and self.outputStep % self.input.tmesh==0:
                    meshtime = time.clock()
                    self.straTIN.write_hdf5_stratigraphy(self.lGIDs,self.outputStep)
                    print("   - Write sediment mesh output %0.02f seconds" % (time.clock() - meshtime))

                if self.carbTIN is not None and self.outputStep % self.input.tmesh==0:
                    meshtime = time.clock()
                    self.carbTIN.write_hdf5_stratigraphy(self.lGIDs,self.outputStep)
                    print("   - Write carbonate mesh output %0.02f seconds" % (time.clock() - meshtime))

                # Update next display time
                last_output = time.clock()
                self.force.next_display += self.input.tDisplay
                self.outputStep += 1
                if self.carbTIN is not None:
                    self.carbTIN.step += 1

            # Update next stratal layer time
            if self.tNow >= self.force.next_layer:
                self.force.next_layer += self.input.laytime
                if self.straTIN is not None:
                    self.straTIN.step += 1
                if self.strata:
                    sub = self.strata.buildStrata(self.elevation, self.cumdiff, self.force.sealevel,
                        self.recGrid.boundsPt,outStrata, self.outputStep-1)
                    self.elevation += sub
                    self.cumdiff += sub
                outStrata = 0

            # Get the maximum time before updating one of the above processes / components
            tStop = min([self.force.next_display, self.force.next_layer, self.force.next_flexure,
                        tEnd, self.force.next_wave, self.force.next_disp, self.force.next_rain,
                        self.next_carbStep])

            print("Returning slopeTIN in model.py")
            self.tNow, self.elevation, self.cumdiff, self.cumhill, self.cumfail, self.slopeTIN = buildFlux.sediment_flux(self.input, self.recGrid, self.hillslope, \
                              self.FVmesh, self.tMesh, self.flow, self.force, self.rain, self.lGIDs, self.applyDisp, self.straTIN, self.mapero,  \
                              self.cumdiff, self.cumhill, self.cumfail, self.fillH, self.disp, self.inGIDs, self.elevation, self.tNow, tStop, verbose)

        tloop = time.clock() - last_time
        print('tNow = %s (%0.02f seconds)' % (self.tNow, tloop))

        # Isostatic flexure
        if self.input.flexure:
            flextime = time.clock()
            ref_elev = buildMesh._get_reference_elevation(self.input, self.recGrid, self.elevation)
            self.force.getSea(self.tNow,self.input.udw,ref_elev)
            self.tinFlex = self.flex.get_flexure(self.elevation, self.cumdiff,
                        self.force.sealevel,self.recGrid.boundsPt,initFlex=False)
            # Get border values
            self.tinFlex = self.force.disp_border(self.tinFlex, self.FVmesh.neighbours,
                                                  self.FVmesh.edge_length, self.recGrid.boundsPt)
            # Update flexural parameters
            self.elevation += self.tinFlex
            self.cumflex += self.tinFlex
            # Update next flexure time
            self.force.next_flexure += self.input.ftime
            print("   - Compute flexural isostasy %0.02f seconds" % (time.clock() - flextime))

        # Create checkpoint files and write HDF5 output
        if self.input.udw == 0 or self.tNow == self.input.tEnd or self.tNow == self.force.next_display:
            checkPoints.write_checkpoints(self.input, self.recGrid, self.lGIDs, self.inIDs, self.tNow, \
                                self.FVmesh, self.tMesh, self.force, self.flow, self.rain, \
                                self.elevation, self.fillH, self.cumdiff, self.cumhill, self.cumfail, self.wavediff, \
                                self.outputStep, self.prop, self.slopeTIN, self.mapero, self.cumflex)
            self.force.next_display += self.input.tDisplay
            self.outputStep += 1
            if self.straTIN is not None:
                self.straTIN.write_hdf5_stratigraphy(self.lGIDs,self.outputStep-1)
            if self.carbTIN is not None:
                self.carbTIN.write_hdf5_stratigraphy(self.lGIDs,self.outputStep-1)
                self.carbTIN.step += 1

        # Update next stratal layer time
        if self.tNow >= self.force.next_layer:
            self.force.next_layer += self.input.laytime
            sub = self.strata.buildStrata(self.elevation, self.cumdiff, self.force.sealevel,
                                    self.recGrid.boundsPt,1, self.outputStep-1)
            self.elevation += sub
            self.cumdiff += sub

        if profile:
            pr.disable()
            s = io.StringIO()
            sortby = 'cumulative'
            ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
            ps.dump_stats('/tmp/profile-%d' % pid)
