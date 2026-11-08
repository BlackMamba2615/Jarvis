// Local optical-photon projection with a curved 3D procedural fallback.
// Per-pixel alpha; no web renderer, text, audio, credentials or microphone.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

internal struct HoloVector
{
    internal double X, Y, Z;
    internal HoloVector(double x, double y, double z) { X = x; Y = y; Z = z; }
    public static HoloVector operator +(HoloVector a, HoloVector b) { return new HoloVector(a.X+b.X, a.Y+b.Y, a.Z+b.Z); }
    public static HoloVector operator -(HoloVector a, HoloVector b) { return new HoloVector(a.X-b.X, a.Y-b.Y, a.Z-b.Z); }
    public static HoloVector operator *(HoloVector a, double s) { return new HoloVector(a.X*s, a.Y*s, a.Z*s); }
    internal static HoloVector Rotate(HoloVector v, double yaw, double pitch, double roll)
    {
        double x = v.X*Math.Cos(yaw)+v.Z*Math.Sin(yaw);
        double z = -v.X*Math.Sin(yaw)+v.Z*Math.Cos(yaw);
        double y = v.Y*Math.Cos(pitch)-z*Math.Sin(pitch);
        z = v.Y*Math.Sin(pitch)+z*Math.Cos(pitch);
        return new HoloVector(x*Math.Cos(roll)-y*Math.Sin(roll), x*Math.Sin(roll)+y*Math.Cos(roll), z);
    }
    internal static HoloVector Cross(HoloVector a, HoloVector b)
    { return new HoloVector(a.Y*b.Z-a.Z*b.Y, a.Z*b.X-a.X*b.Z, a.X*b.Y-a.Y*b.X); }
    internal double Length { get { return Math.Sqrt(X*X+Y*Y+Z*Z); } }
}

internal sealed class HoloPart
{
    internal HoloVector Center, Offset;
    internal double Start, Duration, Roll, Yaw;
    internal readonly List<HoloFace> Faces = new List<HoloFace>();
    internal double Progress(double time) { return HologramRenderer.Clamp((time-Start)/Duration); }
    internal HoloVector Move(HoloVector vertex, double time)
    {
        double p = Progress(time), e = 1-Math.Pow(1-p, 3);
        HoloVector local = HoloVector.Rotate(vertex-Center, Yaw*(1-e), 0.2*(1-e), Roll*(1-e));
        return Center+local*(0.8+0.2*e)+Offset*(1-e);
    }
}

internal sealed class HoloFace
{
    internal HoloVector[] Vertices;
    internal Color Color;
    internal int Seed;
    internal HoloFace(HoloVector[] vertices, Color color, int seed) { Vertices=vertices; Color=color; Seed=seed; }
}

internal sealed class HoloLight
{
    internal HoloPart Part;
    internal HoloVector[] Polygon;
    internal HoloLight(HoloPart part, HoloVector[] polygon) { Part=part; Polygon=polygon; }
}

internal sealed class HologramRenderer : IDisposable
{
    private sealed class OpticalRaster
    {
        internal int Width,Height,Stride;
        internal byte[] Pixels;
    }
    private Bitmap opticalAsset;
    private readonly Dictionary<int,OpticalRaster> opticalRasters=new Dictionary<int,OpticalRaster>();
    private byte[] opticalOutput;
    private sealed class Photon
    {
        internal HoloVector P,N;
        internal HoloPart Part;
        internal int Seed;
    }
    private sealed class Contour
    {
        internal HoloVector[] P;
        internal HoloPart Part;
        internal double Strength;
        internal bool Amber,Closed;
    }
    private sealed class Skin
    {
        internal HoloVector[] P;
        internal HoloPart Part;
    }
    private sealed class FaceDraw
    {
        internal PointF[] P;
        internal double Depth,Alpha;
    }
    private readonly List<HoloPart> parts=new List<HoloPart>();
    private readonly List<Photon> photons=new List<Photon>();
    private readonly List<Contour> contours=new List<Contour>();
    private readonly List<Skin> skins=new List<Skin>();
    private readonly List<HoloLight> lights=new List<HoloLight>();
    private HoloPart chest;
    private int seed;
    internal HologramRenderer() { BuildArmor(); LoadOpticalAsset(); }
    private void LoadOpticalAsset()
    {
        string directory=Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"assets");
        string path=Path.Combine(directory,"jarvis-cinematic-hologram-v2.png");
        if(!File.Exists(path)) return;
        try
        {
            if((File.GetAttributes(directory)&FileAttributes.ReparsePoint)!=0 ||
                (File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0 || new FileInfo(path).Length>12582912) return;
            using(Bitmap source=new Bitmap(path))
            {
                if(source.Width>4096 || source.Height>4096 || source.Width<64 || source.Height<64) return;
                if((source.PixelFormat&PixelFormat.Alpha)==0 || source.GetPixel(0,0).A!=0 || source.GetPixel(source.Width-1,source.Height-1).A!=0) return;
                opticalAsset=new Bitmap(source);
            }
        }
        catch(ArgumentException) { opticalAsset=null; }
        catch(IOException) { opticalAsset=null; }
    }
    private OpticalRaster Raster(int height)
    {
        OpticalRaster cached;
        if(opticalRasters.TryGetValue(height,out cached)) return cached;
        int h=(int)(height*.86),w=(int)(h*(double)opticalAsset.Width/opticalAsset.Height);
        using(Bitmap scaled=new Bitmap(w,h,PixelFormat.Format32bppArgb))
        {
            using(Graphics g=Graphics.FromImage(scaled))
            { g.InterpolationMode=InterpolationMode.HighQualityBicubic; g.DrawImage(opticalAsset,new Rectangle(0,0,w,h)); }
            BitmapData bits=scaled.LockBits(new Rectangle(0,0,w,h),ImageLockMode.ReadOnly,PixelFormat.Format32bppArgb);
            try
            {
                byte[] pixels=new byte[bits.Stride*h]; Marshal.Copy(bits.Scan0,pixels,0,pixels.Length);
                cached=new OpticalRaster { Width=w,Height=h,Stride=bits.Stride,Pixels=pixels };
            }
            finally { scaled.UnlockBits(bits); }
        }
        opticalRasters[height]=cached; return cached;
    }
    public void Dispose()
    { if(opticalAsset!=null) { opticalAsset.Dispose(); opticalAsset=null; } opticalRasters.Clear(); opticalOutput=null; }
    internal static double Clamp(double x) { return Math.Max(0,Math.Min(1,x)); }
    private static double Smooth(double x) { double p=Clamp(x); return p*p*(3-2*p); }
    private static double Lerp(double a,double b,double p) { return a+(b-a)*p; }
    private static HoloVector V(double x,double y,double z) { return new HoloVector(x,y,z); }
    private static HoloVector Unit(HoloVector v) { return v*(1.0/Math.Max(.000001,v.Length)); }
    private static double Noise(int n) { double s=Math.Sin(n*127.13+31.7)*43758.5453; return s-Math.Floor(s); }
    private static double Profile(double[] values,double t)
    {
        double x=Clamp(t)*(values.Length-1); int i=Math.Min(values.Length-2,(int)x); double f=x-i;
        double a=values[Math.Max(0,i-1)],b=values[i],c=values[i+1],d=values[Math.Min(values.Length-1,i+2)];
        return .5*((2*b)+(-a+c)*f+(2*a-5*b+4*c-d)*f*f+(-a+3*b-3*c+d)*f*f*f);
    }
    private HoloPart Part(double x,double y,double z,double start,double dx,double dy,double dz,double roll)
    {
        HoloPart part=new HoloPart { Center=V(x,y,z),Start=start,Duration=1.1,Offset=V(dx,dy,dz),Roll=roll,Yaw=dx*.55 };
        parts.Add(part); return part;
    }
    private void Curve(HoloPart part,HoloVector[] points,double strength,bool amber,bool closed)
    { contours.Add(new Contour { Part=part,P=points,Strength=strength,Amber=amber,Closed=closed }); }
    private void Panel(HoloPart part,HoloVector[] points,double strength)
    {
        Curve(part,points,strength,false,true);
        skins.Add(new Skin { Part=part,P=points });
    }
    private static HoloVector Surface(double[] ys,double[] widths,double[] depths,double x0,double x1,double z,double t,double angle)
    {
        // Smooth cross-sections, with subtle plate ridges rather than a
        // triangulated, extruded cardboard silhouette.
        double ripple=1+.025*Math.Cos(angle*6);
        return V(Lerp(x0,x1,t)+Profile(widths,t)*Math.Cos(angle)*ripple,
            Profile(ys,t),z+Profile(depths,t)*Math.Sin(angle));
    }
    private void Shell(HoloPart part,double[] ys,double[] widths,double[] depths,double x0,double x1,double z,int rows,int sides)
    {
        HoloVector[,] grid=new HoloVector[rows+1,sides];
        for(int r=0;r<=rows;r++)
        {
            double t=(double)r/rows;
            for(int s=0;s<sides;s++)
            {
                double angle=s*Math.PI*2/sides;
                HoloVector p=Surface(ys,widths,depths,x0,x1,z,t,angle);
                HoloVector dy=Surface(ys,widths,depths,x0,x1,z,Math.Min(1,t+.005),angle)-Surface(ys,widths,depths,x0,x1,z,Math.Max(0,t-.005),angle);
                HoloVector da=V(-Profile(widths,t)*Math.Sin(angle),0,Profile(depths,t)*Math.Cos(angle));
                grid[r,s]=p;
                photons.Add(new Photon { P=p,N=Unit(HoloVector.Cross(dy,da)),Part=part,Seed=seed++ });
            }
        }
        for(int r=0;r<=rows;r+=Math.Max(2,rows/11))
        {
            HoloVector[] line=new HoloVector[sides]; for(int s=0;s<sides;s++) line[s]=grid[r,s];
            Curve(part,line,r==0||r==rows?.82:.34,false,true);
        }
        for(int s=0;s<sides;s+=Math.Max(3,sides/10))
        {
            HoloVector[] line=new HoloVector[rows+1]; for(int r=0;r<=rows;r++) line[r]=grid[r,s];
            Curve(part,line,.31,false,false);
        }
        for(int r=0;r<rows;r+=3)
            for(int s=0;s<sides;s+=3)
            {
                int rr=Math.Min(rows,r+3),ss=(s+3)%sides;
                skins.Add(new Skin { Part=part,P=new HoloVector[]{grid[r,s],grid[rr,s],grid[rr,ss],grid[r,ss]} });
            }
    }
    private void Ball(HoloPart part,HoloVector center,double rx,double ry,double rz,int rows,int sides)
    {
        double[] ys=new double[9],widths=new double[9],depths=new double[9];
        for(int i=0;i<9;i++) { double a=-Math.PI/2+i*Math.PI/8; ys[i]=center.Y+ry*Math.Sin(a); widths[i]=Math.Max(.012,rx*Math.Cos(a)); depths[i]=Math.Max(.012,rz*Math.Cos(a)); }
        Shell(part,ys,widths,depths,center.X,center.X,center.Z,rows,sides);
    }
    private void Joint(HoloPart part,HoloVector center,double radius,bool amber)
    {
        for(int ring=0;ring<3;ring++)
        {
            HoloVector[] circle=new HoloVector[64];
            for(int i=0;i<circle.Length;i++)
            {
                double a=i*Math.PI*2/circle.Length;
                circle[i]=center+V(Math.Cos(a)*radius*(1-ring*.19),Math.Sin(a)*radius*(1-ring*.19),ring*.023);
            }
            Curve(part,circle,ring==0?.92:.38,amber,true);
        }
        for(int i=0;i<8;i++)
        {
            double a=i*Math.PI/4;
            Curve(part,new HoloVector[]{center+V(Math.Cos(a)*radius*.82,Math.Sin(a)*radius*.82,.035),center+V(Math.Cos(a)*radius,Math.Sin(a)*radius,.035)},.7,amber,false);
        }
    }
    private void BuildArmor()
    {
        // Curved armor shells over an independent mechanical skeleton. The
        // display is an x-ray projection, with no opaque red/gold low-poly skin.
        chest=Part(0,4.25,0,1.35,0,-.16,.22,0);
        Shell(chest,new double[]{3.23,3.45,3.73,4.02,4.33,4.63,4.86,5.02,5.11},
            new double[]{.34,.38,.41,.48,.64,.79,.76,.52,.24},
            new double[]{.18,.20,.20,.22,.29,.33,.30,.23,.15},0,0,-.015,60,64);
        for(int side=-1;side<=1;side+=2)
        {
            HoloVector[] pectoral=new HoloVector[48];
            for(int i=0;i<48;i++)
            {
                double t=i/47.0;
                pectoral[i]=V(side*(.13+t*.57),4.39+.38*Math.Sin(t*Math.PI*.64),.32-.1*t);
            }
            Curve(chest,pectoral,.95,false,false);
            HoloVector[] lower=new HoloVector[40];
            for(int i=0;i<40;i++) { double t=i/39.0; lower[i]=V(side*(.13+t*.50),4.32-.22*Math.Sin(t*Math.PI*.65),.28-.06*t); }
            Curve(chest,lower,.68,false,false);
            double s=side;
            Panel(chest,new HoloVector[]{V(s*.12,4.91,.30),V(s*.52,4.94,.25),V(s*.72,4.73,.23),V(s*.58,4.46,.29),V(s*.13,4.43,.355)},1.20);
        }
        for(int i=0;i<6;i++)
        {
            double y=3.40+i*.115,w=.32+i*.022;
            HoloVector[] rib=new HoloVector[45];
            for(int k=0;k<45;k++) { double a=Math.PI*k/44; rib[k]=V(w*Math.Cos(a),y-.035*Math.Sin(a),.215*Math.Sin(a)+.025); }
            Curve(chest,rib,.74,false,false);
            Panel(chest,new HoloVector[]{V(-w*.9,y+.045,.19),V(0,y+.065,.245),V(w*.9,y+.045,.19),V(w*.8,y-.035,.20),V(0,y-.055,.25),V(-w*.8,y-.035,.20)},.65);
        }
        HoloPart spine=Part(0,3.85,-.06,.6,0,-.15,-.1,0);
        Shell(spine,new double[]{3.05,3.6,4.2,4.82},new double[]{.10,.12,.12,.11},new double[]{.10,.1,.10,.10},0,0,-.04,28,24);
        for(int i=0;i<9;i++)
        {
            double y=3.38+i*.13;
            Curve(spine,new HoloVector[]{V(-.1,y,.075),V(.1,y,.075)},.60,i%3==0,false);
        }
        HoloPart pelvis=Part(0,3.04,0,1.04,0,-.2,.23,0);
        Shell(pelvis,new double[]{2.77,2.89,3.12,3.29},new double[]{.20,.43,.48,.35},new double[]{.14,.23,.26,.19},0,0,0,25,48);
        Curve(pelvis,new HoloVector[]{V(-.39,3.16,.22),V(-.18,3.13,.28),V(0,3.12,.29),V(.18,3.13,.28),V(.39,3.16,.22)},.9,false,false);
        for(int side=-1;side<=1;side+=2)
        {
            double s=side;
            HoloPart hip=Part(s*.39,2.96,0,.85,s*.14,-.15,.08,-s*.08);
            Joint(hip,V(s*.38,2.98,.23),.16,true);
            HoloPart thigh=Part(s*.43,2.45,0,.72,s*.28,-.16,.24,-s*.12);
            Shell(thigh,new double[]{1.98,2.14,2.47,2.73,2.92},new double[]{.15,.19,.25,.23,.16},
                new double[]{.16,.21,.245,.23,.15},s*.47,s*.37,.0,36,44);
            Panel(thigh,new HoloVector[]{V(s*.27,2.74,.18),V(s*.48,2.79,.21),V(s*.62,2.52,.18),V(s*.52,2.13,.20),V(s*.37,2.09,.19),V(s*.29,2.44,.25)},.98);
            HoloPart knee=Part(s*.47,1.90,0,.54,s*.18,-.13,.18,-s*.08);
            Ball(knee,V(s*.47,1.88,.01),.18,.17,.19,15,36);
            Joint(knee,V(s*.47,1.90,.195),.115,false);
            Panel(knee,new HoloVector[]{V(s*.31,2.0,.18),V(s*.58,2.0,.18),V(s*.65,1.87,.19),V(s*.51,1.73,.21),V(s*.33,1.79,.20)},1.08);
            HoloPart shin=Part(s*.48,1.06,0,.30,s*.28,-.08,.28,-s*.12);
            Shell(shin,new double[]{.35,.56,.91,1.30,1.60,1.75},new double[]{.13,.14,.15,.20,.20,.15},
                new double[]{.14,.16,.20,.235,.23,.18},s*.49,s*.47,.025,38,44);
            Panel(shin,new HoloVector[]{V(s*.34,1.60,.23),V(s*.57,1.62,.23),V(s*.62,1.29,.21),V(s*.55,.48,.19),V(s*.45,.38,.18),V(s*.33,1.26,.22)},1.10);
            for(int detail=0;detail<2;detail++)
            {
                HoloVector[] edge=new HoloVector[32];
                for(int k=0;k<32;k++) { double t=k/31.0; edge[k]=V(s*(.48+(detail==0?-.06:.06)),.45+t*1.12,.19+.035*Math.Sin(t*Math.PI)); }
                Curve(shin,edge,.85,false,false);
            }
            HoloPart boot=Part(s*.49,.2,.18,.10,s*.16,0,.27,-s*.07);
            Ball(boot,V(s*.49,.19,.22),.185,.17,.37,15,40);
            HoloVector[] sole=new HoloVector[56];
            for(int i=0;i<56;i++) { double a=i*Math.PI*2/56; sole[i]=V(s*.49+.18*Math.Cos(a),.07,.23+.37*Math.Sin(a)); }
            Curve(boot,sole,.95,false,true);
            HoloPart shoulder=Part(s*.88,4.84,0,2.3,s*.35,.1,.20,-s*.25);
            Ball(shoulder,V(s*.91,4.79,-.015),.28,.25,.28,19,48);
            Joint(shoulder,V(s*.92,4.80,.20),.135,true);
            Panel(shoulder,new HoloVector[]{V(s*.73,4.97,.14),V(s*.96,5.03,.16),V(s*1.15,4.84,.16),V(s*1.1,4.66,.19),V(s*.87,4.62,.25),V(s*.71,4.78,.17)},1.10);
            HoloPart upper=Part(s*1.08,4.27,0,2.2,s*.34,.02,.20,-s*.20);
            Shell(upper,new double[]{3.96,4.08,4.31,4.53,4.67},new double[]{.12,.15,.185,.18,.13},
                new double[]{.135,.18,.19,.19,.14},s*1.17,s*1.00,-.015,26,40);
            HoloPart elbow=Part(s*1.18,3.86,0,2.45,s*.28,0,.18,-s*.15);
            Ball(elbow,V(s*1.18,3.86,0),.14,.14,.15,12,32);
            Joint(elbow,V(s*1.18,3.86,.155),.095,true);
            HoloPart forearm=Part(s*1.25,3.50,0,2.60,s*.35,-.05,.24,-s*.18);
            Shell(forearm,new double[]{3.12,3.25,3.48,3.68,3.77},new double[]{.11,.13,.18,.17,.12},
                new double[]{.12,.16,.205,.20,.135},s*1.29,s*1.19,.025,27,44);
            Panel(forearm,new HoloVector[]{V(s*1.13,3.69,.17),V(s*1.29,3.71,.20),V(s*1.40,3.49,.18),V(s*1.34,3.20,.14),V(s*1.22,3.20,.17),V(s*1.13,3.46,.21)},1.1);
            HoloPart hand=Part(s*1.29,2.97,.04,2.9,s*.25,-.08,.28,-s*.14);
            Ball(hand,V(s*1.29,2.98,.05),.13,.16,.12,14,32);
            for(int finger=0;finger<4;finger++)
            {
                double x=s*(1.205+finger*.055),end=2.65+Math.Abs(finger-1.3)*.025;
                Shell(hand,new double[]{end,end+.055,2.84,2.94},new double[]{.017,.024,.024,.022},new double[]{.021,.028,.028,.023},x,x,.075,9,12);
            }
            Curve(hand,new HoloVector[]{V(s*1.17,3.04,.05),V(s*1.09,2.95,.09),V(s*1.10,2.87,.10)},.9,false,false);
            Joint(hand,V(s*1.29,3.01,.175),.065,false);
            lights.Add(new HoloLight(hand,new HoloVector[]{V(s*1.25,3.05,.182),V(s*1.33,3.05,.182),V(s*1.33,2.97,.182),V(s*1.25,2.97,.182)}));
        }
        HoloPart neck=Part(0,5.25,0,3.25,0,.12,.06,0);
        Shell(neck,new double[]{5.02,5.12,5.27,5.42},new double[]{.30,.25,.20,.18},new double[]{.20,.18,.16,.15},0,0,-.025,15,36);
        HoloPart helmet=Part(0,5.83,0,3.7,0,.28,.18,.08);
        Shell(helmet,new double[]{5.36,5.45,5.62,5.86,6.08,6.23,6.29},new double[]{.14,.24,.32,.36,.33,.23,.045},
            new double[]{.15,.23,.285,.32,.285,.19,.05},0,0,0,39,64);
        HoloPart mask=Part(0,5.82,.28,4.1,0,.15,.52,.06);
        mask.Duration=.95;
        Panel(mask,new HoloVector[]{V(-.27,6.13,.20),V(-.12,6.10,.30),V(0,5.99,.35),V(.12,6.10,.30),V(.27,6.13,.20),
            V(.33,5.89,.23),V(.28,5.61,.25),V(.13,5.43,.28),V(-.13,5.43,.28),V(-.28,5.61,.25),V(-.33,5.89,.23)},1.3);
        for(int side=-1;side<=1;side+=2)
            Panel(mask,new HoloVector[]{V(side*.30,5.78,.30),V(side*.14,5.71,.38),V(side*.08,5.56,.385),V(side*.16,5.51,.34),V(side*.26,5.65,.31)},1.05);
        Curve(mask,new HoloVector[]{V(-.25,5.69,.30),V(-.12,5.58,.36),V(0,5.54,.385),V(.12,5.58,.36),V(.25,5.69,.30)},.95,false,false);
        Curve(mask,new HoloVector[]{V(-.14,5.48,.33),V(0,5.46,.36),V(.14,5.48,.33)},.95,false,false);
        for(int row=0;row<25;row++)
        {
            double y=5.48+row*.024,half=.13+.14*Math.Sin((y-5.48)/.6*Math.PI*.8);
            for(int col=0;col<30;col++)
            {
                double x=-half+2*half*col/29.0,z=.24+.135*(1-x*x/.12);
                photons.Add(new Photon { Part=mask,P=V(x,y,z),N=Unit(V(x*.3,.05,1)),Seed=seed++ });
            }
        }
        lights.Add(new HoloLight(mask,new HoloVector[]{V(-.28,5.87,.37),V(-.065,5.83,.415),V(-.085,5.775,.415),V(-.25,5.81,.38)}));
        lights.Add(new HoloLight(mask,new HoloVector[]{V(.065,5.83,.415),V(.28,5.87,.37),V(.25,5.81,.38),V(.085,5.775,.415)}));
        Joint(chest,V(0,4.49,.355),.155,true);
        // Thin internal conduits make the shell look volumetric through the
        // opposite side, without adding surrounding fireworks or labels.
        for(int side=-1;side<=1;side+=2)
        {
            HoloVector[] conduit=new HoloVector[54];
            for(int i=0;i<54;i++) { double t=i/53.0; conduit[i]=V(side*(.12+.15*Math.Sin(t*Math.PI)),3.42+t*1.22,.055); }
            Curve(spine,conduit,.78,true,false);
        }
    }
    private HoloVector Camera(HoloVector p,double time)
    { return HoloVector.Rotate(p-V(0,3.1-.025*Math.Sin(time*1.2),0),-.43+.16*Math.Sin(time*.5),.035,0); }
    private HoloVector Normal(HoloVector n,HoloPart part,double time)
    {
        double e=1-Math.Pow(1-part.Progress(time),3);
        return HoloVector.Rotate(HoloVector.Rotate(n,part.Yaw*(1-e),.2*(1-e),part.Roll*(1-e)),-.43+.16*Math.Sin(time*.5),.035,0);
    }
    private PointF Project(HoloVector p,int w,int h)
    {
        double scale=h/8.5*9/(9-p.Z);
        return new PointF((float)(w*.5+p.X*scale),(float)(h*.515-p.Y*scale));
    }
    private PointF Screen(HoloVector p,double time,int w,int h) { return Project(Camera(p,time),w,h); }
    private static Color Ink(double alpha,int r,int g,int b) { return Color.FromArgb((int)(255*Clamp(alpha)),r,g,b); }
    private static void Glow(Graphics g,PointF c,float rx,float ry,Color color,double opacity)
    {
        if(opacity<=.001 || rx<=0 || ry<=0) return;
        using(GraphicsPath path=new GraphicsPath())
        {
            path.AddEllipse(c.X-rx,c.Y-ry,rx*2,ry*2);
            using(PathGradientBrush brush=new PathGradientBrush(path))
            {
                brush.CenterColor=Ink(opacity,color.R,color.G,color.B);
                brush.SurroundColors=new Color[]{Color.FromArgb(0,color)}; g.FillPath(brush,path);
            }
        }
    }
    private double Reveal(HoloPart part,double y,double time)
    {
        double progress=Smooth(part.Progress(time));
        double scan=-.2+Smooth((time-.25)/4.8)*6.9;
        return (.025+.975*Smooth((scan-y+.22)/.40))*progress;
    }
    internal Bitmap Render(double time,int width,int height,bool ready)
    {
        if(opticalAsset!=null) return RenderOptical(time,width,height,ready);
        Bitmap image=new Bitmap(width,height,PixelFormat.Format32bppPArgb);
        using(Graphics g=Graphics.FromImage(image))
        {
            g.Clear(Color.Transparent); g.SmoothingMode=SmoothingMode.AntiAlias;
            g.CompositingQuality=CompositingQuality.HighQuality;
            double fade=Smooth(time/.55),scan=-.2+Smooth((time-.25)/4.8)*6.9;
            Projection(g,time,width,height,fade);
            List<FaceDraw> volumes=new List<FaceDraw>();
            foreach(Skin skin in skins)
            {
                PointF[] polygon=new PointF[skin.P.Length]; double depth=0,y=0;
                for(int i=0;i<polygon.Length;i++)
                {
                    HoloVector v=Camera(skin.Part.Move(skin.P[i],time),time);
                    polygon[i]=Project(v,width,height); depth+=v.Z/polygon.Length; y+=skin.P[i].Y/polygon.Length;
                }
                volumes.Add(new FaceDraw { P=polygon,Depth=depth,Alpha=fade*Reveal(skin.Part,y,time)*.085 });
            }
            volumes.Sort(delegate(FaceDraw a,FaceDraw b){return a.Depth.CompareTo(b.Depth);});
            foreach(FaceDraw face in volumes)
                if(face.Alpha>.002) using(SolidBrush brush=new SolidBrush(Ink(face.Alpha,1,106,154))) g.FillPolygon(brush,face.P);

            // Sparse curved contour families replace the old triangle grid.
            // Visible rear contours are deliberately dimmer x-ray structure.
            foreach(Contour curve in contours)
            {
                PointF[] points=new PointF[curve.P.Length]; double y=0,depth=0;
                for(int i=0;i<points.Length;i++)
                {
                    HoloVector v=Camera(curve.Part.Move(curve.P[i],time),time);
                    points[i]=Project(v,width,height); y+=curve.P[i].Y/points.Length; depth+=v.Z/points.Length;
                }
                double reveal=Reveal(curve.Part,y,time),alpha=fade*reveal*curve.Strength;
                if(alpha<.015) continue;
                int rr=curve.Amber?255:57,gg=curve.Amber?196:209,bb=curve.Amber?85:247;
                double front=.65+.35*Clamp((depth+.25)/.75);
                using(Pen bloom=new Pen(Ink(alpha*.08,rr,gg,bb),3.6f)) DrawCurve(g,bloom,points,curve.Closed);
                // A blue optical core remains legible over a white desktop.
                using(Pen core=new Pen(Ink(alpha*.25*front,curve.Amber?167:0,curve.Amber?121:99,curve.Amber?36:162),1.10f)) DrawCurve(g,core,points,curve.Closed);
                using(Pen light=new Pen(Ink(alpha*.82*front,rr,gg,bb),.68f)) DrawCurve(g,light,points,curve.Closed);
            }

            List<RectangleF>[] bins=new List<RectangleF>[16];
            for(int i=0;i<16;i++) bins[i]=new List<RectangleF>();
            for(int i=0;i<photons.Count;i++)
            {
                Photon photon=photons[i]; double noise=Noise(photon.Seed);
                if(noise<.40) continue;
                double phase=photon.Part.Progress(time),reveal=Reveal(photon.Part,photon.P.Y,time);
                if(reveal<.015) continue;
                HoloVector p=photon.Part.Move(photon.P,time);
                double flow=(1-Smooth(phase))*.11;
                p+=V(flow*Math.Sin(photon.Seed+time*2.0),flow*Math.Cos(photon.Seed*.31+time),flow*Math.Sin(time+photon.Seed*.7));
                HoloVector view=Camera(p,time),normal=Normal(photon.N,photon.Part,time);
                double facing=Math.Max(0,normal.Z),rim=Math.Pow(1-Math.Abs(normal.Z),2.0);
                double layer=.20+.38*facing+.30*rim;
                double scanBand=Math.Exp(-Math.Pow((photon.P.Y-scan)/.11,2));
                double intensity=fade*reveal*(layer+scanBand*.50)*(.67+.33*noise);
                double sparkle=.91+.09*Math.Sin(time*5.2+photon.Seed*.13);
                int bin=(int)(Clamp(intensity*sparkle/.60)*15);
                if(bin<1) continue;
                PointF screen=Project(view,width,height);
                float size=(float)(height/960.0*(.72+.6*rim+.55*scanBand));
                bins[bin].Add(new RectangleF(screen.X,screen.Y,size,size));
            }
            for(int i=1;i<16;i++)
            {
                double q=i/15.0;
                using(SolidBrush brush=new SolidBrush(Ink(q*.72,(int)Lerp(12,192,q),(int)Lerp(155,249,q),255)))
                    if(bins[i].Count>0) g.FillRectangles(brush,bins[i].ToArray());
            }
            MaterialStreams(g,time,width,height,fade);
            Illuminate(g,time,width,height,ready,fade);
        }
        return image;
    }
    private Bitmap RenderOptical(double time,int width,int height,bool ready)
    {
        // Image-based optical projection: preserve the original high-detail
        // asset, then animate its photons in memory with scan/flow and a small
        // curved depth-field parallax. This is not a downloadable 3D mesh.
        OpticalRaster raster=Raster(height);
        int stride=width*4,length=stride*height;
        if(opticalOutput==null || opticalOutput.Length!=length) opticalOutput=new byte[length];
        else Array.Clear(opticalOutput,0,length);
        int left=(width-raster.Width)/2,top=(height-raster.Height)/2;
        double fade=Smooth(time/.55),parallax=Math.Sin(time*.55)*4.5;
        double blink=1-.63*Math.Exp(-Math.Pow(((time+1.0)%4.1-3.2)/.07,2));
        double[] depths=new double[raster.Width],flow=new double[raster.Width],eyeLeft=new double[raster.Width],eyeRight=new double[raster.Width],coreX=new double[raster.Width],edgeX=new double[raster.Width];
        for(int x=0;x<raster.Width;x++)
        {
            double u=x/(double)raster.Width;
            depths[x]=Math.Sqrt(Math.Max(0,1-Math.Pow((u-.53)/.52,2)));
            flow[x]=Math.Sin(x*.027+time*3.8);
            eyeLeft[x]=Math.Pow((u-.504)/.029,2); eyeRight[x]=Math.Pow((u-.566)/.029,2);
            coreX[x]=Math.Pow((u-.56)/.039,2);
            edgeX[x]=Smooth(Math.Min(x,raster.Width-1-x)/(raster.Width*.05));
        }
        for(int y=0;y<raster.Height;y++)
        {
            double v=y/(double)raster.Height;
            double p=Smooth((time-.18-(1-v)*4.30)/.78);
            if(p<=0) continue;
            double band=Math.Exp(-Math.Pow((time-.18-(1-v)*4.30-.38)/.14,2));
            double flutter=1+.025*Math.Sin(y*.09+time*2.5);
            double eyeYLeft=Math.Pow((v-.070)/.009,2),eyeYRight=Math.Pow((v-.073)/.009,2),coreY=Math.Pow((v-.207)/.024,2);
            double edgeY=Smooth(Math.Min(y,raster.Height-1-y)/(raster.Height*.018));
            for(int x=0;x<raster.Width;x++)
            {
                int src=y*raster.Stride+x*4;
                int originalAlpha=raster.Pixels[src+3];
                if(originalAlpha==0) continue;
                uint hash=unchecked((uint)((x/2)*374761393+(y/2)*668265263));
                hash=(hash^(hash>>13))*1274126177u;
                double grain=(hash&65535)/65535.0;
                if(p<grain) continue;
                int dx=left+x+(int)(parallax*depths[x]+(1-p)*14*flow[x]);
                int dy=top+y+(int)(Math.Sin(time*1.25)*1.7+(1-p)*9);
                if(dx<0 || dx>=width || dy<0 || dy>=height) continue;
                double eye=Math.Min(eyeLeft[x]+eyeYLeft,eyeRight[x]+eyeYRight);
                double brightness=flutter*(1+.16*band);
                if(eye<1 && raster.Pixels[src+1]>195 && raster.Pixels[src+2]>100)
                    brightness*=ready?blink:.58;
                double core=coreX[x]+coreY;
                if(core<1) brightness*=1+.07*Math.Sin(time*3.2);
                int alpha=(int)Math.Min(255,originalAlpha*fade*Smooth(p)*brightness*edgeX[x]*edgeY);
                int dest=dy*stride+dx*4;
                if(alpha<opticalOutput[dest+3]) continue;
                opticalOutput[dest]=(byte)(Math.Min(255,raster.Pixels[src]*brightness)*alpha/255);
                opticalOutput[dest+1]=(byte)(Math.Min(255,raster.Pixels[src+1]*brightness)*alpha/255);
                opticalOutput[dest+2]=(byte)(Math.Min(255,raster.Pixels[src+2]*brightness)*alpha/255);
                opticalOutput[dest+3]=(byte)alpha;
            }
        }
        Bitmap image=new Bitmap(width,height,PixelFormat.Format32bppPArgb);
        BitmapData output=image.LockBits(new Rectangle(0,0,width,height),ImageLockMode.WriteOnly,PixelFormat.Format32bppPArgb);
        try { Marshal.Copy(opticalOutput,0,output.Scan0,length); }
        finally { image.UnlockBits(output); }
        return image;
    }
    private static void DrawCurve(Graphics g,Pen pen,PointF[] points,bool closed)
    { if(closed) g.DrawPolygon(pen,points); else g.DrawLines(pen,points); }

    private void Projection(Graphics g,double time,int width,int height,double fade)
    {
        PointF origin=Screen(V(0,-.15,.1),time,width,height);
        Glow(g,origin,width*.19f,height*.025f,Color.FromArgb(20,168,229),fade*.13);
        using(Pen pen=new Pen(Ink(fade*.12,25,159,198),.65f))
        {
            for(int i=-2;i<=2;i++)
            {
                g.DrawLine(pen,Screen(V(-1.1,-.16,i*.3),time,width,height),Screen(V(1.1,-.16,i*.3),time,width,height));
                g.DrawLine(pen,Screen(V(i*.45,-.16,-.6),time,width,height),Screen(V(i*.45,-.16,.6),time,width,height));
            }
        }
        for(int i=0;i<7;i++)
        {
            double a=i*.88;
            PointF top=Screen(V(.9*Math.Sin(a),5.9,-.35+.25*Math.Cos(a)),time,width,height);
            using(Pen beam=new Pen(Ink(.013*fade,67,215,248),1.0f))
                g.DrawLine(beam,origin,top);
        }
    }
    private void MaterialStreams(Graphics g,double time,int width,int height,double fade)
    {
        if(time>5.3) return;
        for(int i=0;i<90;i++)
        {
            Photon target=photons[(i*277+43)%photons.Count];
            double p=target.Part.Progress(time);
            if(p<=.02 || p>=.98) continue;
            double u=Noise(i*71),q=Clamp(p*1.1+u*.25);
            HoloVector goal=target.Part.Move(target.P,time);
            HoloVector source=goal+V((u-.5)*.40,-.40-.20*u,.15*Math.Sin(i));
            HoloVector point=source*(1-q)+goal*q;
            point.X+=(1-q)*.07*Math.Sin(q*7+i);
            PointF s=Screen(point,time,width,height);
            using(SolidBrush brush=new SolidBrush(Ink(fade*.28*Math.Sin(q*Math.PI),103,234,255)))
                g.FillRectangle(brush,s.X,s.Y,1.15f,1.15f);
        }
    }
    private void Illuminate(Graphics g,double time,int width,int height,bool ready,double fade)
    {
        double powered=ready?Smooth((time-4.75)/.65):.20;
        double pulse=.86+.14*Math.Sin(time*2.6);
        foreach(HoloLight light in lights)
        {
            PointF[] polygon=new PointF[light.Polygon.Length]; PointF center=new PointF();
            for(int i=0;i<polygon.Length;i++)
            {
                polygon[i]=Screen(light.Part.Move(light.Polygon[i],time),time,width,height);
                center.X+=polygon[i].X/polygon.Length; center.Y+=polygon[i].Y/polygon.Length;
            }
            double visible=fade*powered*Smooth((light.Part.Progress(time)-.75)/.25);
            float size=Math.Max(2,Math.Abs(polygon[1].X-polygon[0].X));
            Glow(g,center,size*1.35f,size*.75f,Color.FromArgb(92,221,255),visible*.40);
            using(SolidBrush brush=new SolidBrush(Ink(visible*pulse,202,249,255))) g.FillPolygon(brush,polygon);
        }
        double on=fade*Reveal(chest,4.49,time),energy=on*(.3+.7*powered);
        HoloVector reactor=chest.Move(V(0,4.49,.40),time);
        PointF centerPoint=Screen(reactor,time,width,height);
        float radius=height/8.5f*.105f;
        Glow(g,centerPoint,radius*4.6f,radius*4.6f,Color.FromArgb(53,205,255),energy*.45);
        using(Pen ring=new Pen(Ink(on*.8,155,237,255),1.0f)) g.DrawEllipse(ring,centerPoint.X-radius,centerPoint.Y-radius,radius*2,radius*2);
        using(SolidBrush core=new SolidBrush(Ink(energy*.90,192,246,255))) g.FillEllipse(core,centerPoint.X-radius*.66f,centerPoint.Y-radius*.66f,radius*1.32f,radius*1.32f);
    }
    internal static void RenderFrames(string directory)
    {
        string root=Path.GetFullPath(directory); Directory.CreateDirectory(root);
        HologramRenderer renderer=new HologramRenderer();
        double[] times={.6,1.7,2.9,4.25,5.75,6.8};
        for(int i=0;i<times.Length;i++)
        {
            string path=Path.Combine(root,"armor-"+i.ToString("00")+".png");
            if(File.Exists(path)) throw new IOException("Preview frame already exists");
            using(Bitmap image=renderer.Render(times[i],800,960,true))
            {
                image.Save(path,ImageFormat.Png);
                using(Bitmap review=new Bitmap(800,960,PixelFormat.Format32bppArgb))
                using(Graphics g=Graphics.FromImage(review))
                {
                    g.Clear(Color.FromArgb(8,17,27)); g.DrawImageUnscaled(image,0,0);
                    review.Save(Path.Combine(root,"review-"+i.ToString("00")+".png"),ImageFormat.Png);
                }
            }
        }
    }
    internal static void RenderAnimation(string directory)
    {
        string root=Path.GetFullPath(directory); Directory.CreateDirectory(root);
        HologramRenderer renderer=new HologramRenderer();
        for(int i=0;i<156;i++)
        {
            string path=Path.Combine(root,"frame-"+i.ToString("0000")+".png");
            if(File.Exists(path)) throw new IOException("Preview frame already exists");
            using(Bitmap image=renderer.Render(i/20.0,500,600,true))
            using(Bitmap review=new Bitmap(500,600,PixelFormat.Format32bppArgb))
            using(Graphics g=Graphics.FromImage(review))
            {
                g.Clear(Color.FromArgb(8,17,27)); g.DrawImageUnscaled(image,0,0); review.Save(path,ImageFormat.Png);
            }
        }
    }
    internal static void Check()
    {
        HologramRenderer renderer=new HologramRenderer();
        if(renderer.parts.Count<20 || renderer.lights.Count!=4 || renderer.photons.Count<10000 || renderer.contours.Count<200)
            throw new InvalidOperationException("Incomplete hologram geometry");
        foreach(Photon photon in renderer.photons)
            if(Double.IsNaN(photon.P.X+photon.P.Y+photon.P.Z+photon.N.X+photon.N.Y+photon.N.Z))
                throw new InvalidOperationException("Invalid curved surface");
        foreach(double t in new double[]{0,1,3,5.8,10})
            using(Bitmap image=renderer.Render(t,360,440,true))
                if(image.GetPixel(0,0).A!=0 || image.GetPixel(359,439).A!=0)
                    throw new InvalidOperationException("Hologram background is not transparent");
    }
}

internal sealed class HologramStartupForm : Form
{
    [StructLayout(LayoutKind.Sequential)] private struct POINT { internal int X,Y; internal POINT(int x,int y){X=x;Y=y;} }
    [StructLayout(LayoutKind.Sequential)] private struct SIZE { internal int X,Y; internal SIZE(int x,int y){X=x;Y=y;} }
    [StructLayout(LayoutKind.Sequential,Pack=1)] private struct BLEND { internal byte Operation,Flags,Alpha,Format; }
    [DllImport("user32.dll")] private static extern IntPtr GetDC(IntPtr h);
    [DllImport("user32.dll")] private static extern int ReleaseDC(IntPtr h,IntPtr dc);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleDC(IntPtr dc);
    [DllImport("gdi32.dll")] private static extern bool DeleteDC(IntPtr dc);
    [DllImport("gdi32.dll")] private static extern IntPtr SelectObject(IntPtr dc,IntPtr obj);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr obj);
    [DllImport("user32.dll",SetLastError=true)] private static extern bool UpdateLayeredWindow(IntPtr h,IntPtr screen,ref POINT location,ref SIZE size,IntPtr source,ref POINT origin,uint key,ref BLEND blend,uint flags);

    private readonly HologramRenderer renderer=new HologramRenderer();
    private readonly Stopwatch clock=new Stopwatch();
    private readonly Timer timer=new Timer();
    private readonly Func<bool> ready;
    private readonly Func<bool> stopped;
    private readonly bool hold;
    private double lastReadyCheck;
    private bool isReady;

    internal HologramStartupForm(Func<bool> readiness,bool holdPreview,Func<bool> stoppedListener=null)
    {
        ready=readiness; stopped=stoppedListener; hold=holdPreview; Text="Jarvis Holographic Startup";
        FormBorderStyle=FormBorderStyle.None; ShowInTaskbar=holdPreview; TopMost=true;
        StartPosition=FormStartPosition.Manual;
        Rectangle screen=Screen.PrimaryScreen.Bounds;
        int height=Math.Min(960,(int)(screen.Height*.79));
        ClientSize=new Size((int)(height*.84),height);
        Location=new Point(screen.Left+(screen.Width-Width)/2,screen.Top+(screen.Height-Height)/2);
        timer.Interval=33; timer.Tick+=Tick;
        KeyPreview=true;
        KeyDown+=delegate(object sender,KeyEventArgs e) { if(hold && e.KeyCode==Keys.Escape) Close(); };
    }
    protected override bool ShowWithoutActivation { get { return true; } }
    protected override CreateParams CreateParams
    {
        get { CreateParams cp=base.CreateParams; cp.ExStyle|=0x80000; if(!hold) cp.ExStyle|=0x80|0x08000000|0x20; return cp; }
    }
    protected override void OnShown(EventArgs e)
    { base.OnShown(e); clock.Start(); timer.Start(); Tick(this,EventArgs.Empty); }
    private void Tick(object sender,EventArgs e)
    {
        double seconds=clock.Elapsed.TotalSeconds;
        if(seconds-lastReadyCheck>.25)
        {
            lastReadyCheck=seconds;
            try
            {
                if(stopped!=null && stopped()) { Close(); return; }
                isReady=ready==null || ready();
            }
            catch { isReady=false; }
        }
        if((!hold && seconds>=7.8 && isReady) || seconds>(hold?120:24)) { Close(); return; }
        double t=hold?Math.Min(seconds,5.9):seconds;
        double opacity=(!hold && seconds>7.0 && isReady)?1-HologramRenderer.Clamp((seconds-7.0)/.8):1;
        using(Bitmap bitmap=renderer.Render(t,Width,Height,isReady)) Present(bitmap,(byte)(255*opacity));
    }
    private void Present(Bitmap bitmap,byte opacity)
    {
        IntPtr screen=GetDC(IntPtr.Zero),memory=IntPtr.Zero,pixels=IntPtr.Zero,previous=IntPtr.Zero;
        try
        {
            memory=CreateCompatibleDC(screen); pixels=bitmap.GetHbitmap(Color.FromArgb(0)); previous=SelectObject(memory,pixels);
            POINT location=new POINT(Left,Top),origin=new POINT(0,0); SIZE size=new SIZE(Width,Height);
            BLEND blend=new BLEND { Operation=0,Flags=0,Alpha=opacity,Format=1 };
            if(!UpdateLayeredWindow(Handle,screen,ref location,ref size,memory,ref origin,0,ref blend,2))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        }
        finally
        {
            if(previous!=IntPtr.Zero) SelectObject(memory,previous);
            if(pixels!=IntPtr.Zero) DeleteObject(pixels);
            if(memory!=IntPtr.Zero) DeleteDC(memory);
            if(screen!=IntPtr.Zero) ReleaseDC(IntPtr.Zero,screen);
        }
    }
    protected override void Dispose(bool disposing)
    { if(disposing) { timer.Stop(); timer.Dispose(); renderer.Dispose(); } base.Dispose(disposing); }
}
